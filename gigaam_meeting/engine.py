"""Bounded, memory-only CPU GigaAM inference. No model-hub loader or FFmpeg."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sys
import threading

from .contracts import Word

SAMPLE_RATE = 16000
MAX_SECONDS = 300
MODEL_REVISION = "2f8a57144e6ec3adfd32fe0484d9ea9913305bc8"
CODE_REVISION = "7447938d791c4f3e643386ee22c33777004293a5"
MODEL_FILES = {
    "config.json": "c830232c7d51688a630a221517b52585ab5ee57e1d3c21bcbae01759351d2653",
    "pytorch_model.bin": "e1db43873ec5e296f229572e06e2470fc157ac9f8d4aacabda295630b9b91728",
}


class EngineError(RuntimeError):
    """Safe, actionable message for the user; contains no audio or transcript."""


def bundle_root():
    return Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent


def verify_model(directory):
    for name, expected in MODEL_FILES.items():
        path = Path(directory) / name
        if not path.is_file():
            raise EngineError("Модель не найдена. Распакуйте весь ZIP, включая папку models.")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise EngineError("Файлы модели повреждены. Заново распакуйте проверенный ZIP.")


def windows(total):
    """24-second inputs, 1-second overlap, disjoint timestamp ownership."""
    size, overlap = 24 * SAMPLE_RATE, SAMPLE_RATE
    start = 0
    while start < total:
        end = min(start + size, total)
        yield start, end, (0 if start == 0 else start + overlap / 2) / SAMPLE_RATE, (total if end == total else end - overlap / 2) / SAMPLE_RATE
        if end == total:
            break
        start += size - overlap


class Transcriber:
    def __init__(self, model_dir=None):
        self.model_dir = Path(model_dir) if model_dir else bundle_root() / "models" / "gigaam"
        self.model = None
        self.lock = threading.Lock()

    def load(self):
        if self.model is not None:
            return
        verify_model(self.model_dir)
        import torch
        from gigaam.model import GigaAMASR
        from omegaconf import OmegaConf
        torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
        config = json.loads((self.model_dir / "config.json").read_text(encoding="utf-8"))["cfg"]["model"]["cfg"]
        allowed = {
            "preprocessor": ("modeling_gigaam.FeatureExtractor", "gigaam.preprocess.FeatureExtractor"),
            "encoder": ("modeling_gigaam.ConformerEncoder", "gigaam.encoder.ConformerEncoder"),
            "head": ("modeling_gigaam.CTCHead", "gigaam.decoder.CTCHead"),
            "decoding": ("modeling_gigaam.CTCGreedyDecoding", "gigaam.decoding.CTCGreedyDecoding"),
        }
        for section, (original, replacement) in allowed.items():
            if config[section].get("_target_") != original:
                raise EngineError("Неподдерживаемая конфигурация модели.")
            config[section]["_target_"] = replacement
        config["encoder"]["flash_attn"] = False
        model = GigaAMASR(OmegaConf.create(config))
        state = torch.load(self.model_dir / "pytorch_model.bin", map_location="cpu", weights_only=True)
        model.load_state_dict({k.removeprefix("model."): v for k, v in state.items()}, strict=True)
        del state
        self.model = model.eval().to("cpu")

    def transcribe_words(self, samples, sample_rate) -> list[Word]:
        """Return words on this input's clock without writing or retaining audio."""
        import numpy as np
        if not isinstance(sample_rate, int) or not 8000 <= sample_rate <= 192000:
            raise EngineError("Неподдерживаемая частота микрофона.")
        samples = np.asarray(samples, dtype=np.float32)
        if samples.ndim != 1 or len(samples) > MAX_SECONDS * sample_rate or not np.isfinite(samples).all():
            raise EngineError("Запись повреждена или превышает 5 минут.")
        if len(samples) == 0 or not np.any(samples):
            return []
        with self.lock:
            import torch
            import torchaudio.functional as audio
            self.load()
            values = torch.from_numpy(samples.copy())
            if sample_rate != SAMPLE_RATE:
                values = audio.resample(values, sample_rate, SAMPLE_RATE)
            result = []
            for start, end, own_start, own_end in windows(len(values)):
                clip = values[start:end]
                if not torch.any(clip):
                    continue
                if len(clip) < SAMPLE_RATE // 2:
                    clip = torch.nn.functional.pad(clip, (0, SAMPLE_RATE // 2 - len(clip)))
                wav = clip.unsqueeze(0)
                lengths = torch.tensor([wav.shape[-1]])
                with torch.inference_mode():
                    encoded, encoded_lengths = self.model.forward(wav, lengths)
                    text, words = self.model._decode(encoded, encoded_lengths, lengths, word_timestamps=True)[0]
                if not text.strip():
                    continue
                if not words:
                    # A streaming seam cannot be merged safely without words.
                    raise EngineError("Модель не вернула метки слов. Повторите запись.")
                for word in words:
                    a, b = float(word.start) + start / SAMPLE_RATE, float(word.end) + start / SAMPLE_RATE
                    if not math.isfinite(a) or not math.isfinite(b) or b < a:
                        raise EngineError("Некорректные метки времени модели.")
                    if own_start <= (a + b) / 2 < own_end:
                        value = word.text.strip()
                        if value:
                            result.append(Word(max(a, own_start), min(b, own_end), value))
            return result

    def transcribe(self, samples, sample_rate):
        """Compatibility with the verified microphone application's string API."""
        return " ".join(word.text for word in self.transcribe_words(samples, sample_rate))
