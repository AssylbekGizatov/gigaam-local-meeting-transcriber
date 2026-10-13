"""Offline public RU/EN evaluation; no hardware or natural code-switch claim."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gigaam_meeting.offline import install, blocked_attempts  # noqa: E402
install()


def distance(reference, hypothesis):
    row = list(range(len(hypothesis) + 1))
    for index, word in enumerate(reference, 1):
        previous, row = row, [index]
        for offset, other in enumerate(hypothesis, 1):
            row.append(min(row[-1] + 1, previous[offset] + 1, previous[offset - 1] + (word != other)))
    return row[-1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.report.exists():
        raise FileExistsError("Choose a new report")
    import numpy as np
    from gigaam_meeting.engine import Transcriber
    from gigaam_meeting.selftest import EXPECTED_TEXT
    from gigaam_meeting.contracts import AudioBlock
    from gigaam_meeting.streaming import StreamProcessor
    from gigaam.utils import normalize_raw_text

    def read(path, expected):
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise RuntimeError("Fixture SHA256 mismatch")
        with wave.open(str(path)) as file:
            assert file.getframerate() == 16000 and file.getnchannels() == 1 and file.getsampwidth() == 2
            return np.frombuffer(file.readframes(file.getnframes()), dtype="<i2").astype(np.float32) / 32768

    russian = read(ROOT / "assets/asr-smoke.wav", "d8aaaa18a5098d7c6de0595ae7ac1e64cacd0d4022af3595213bdaf23be77e69")
    english = read(ROOT / "build/language-fixtures/english-librispeech/en_us.wav", "799f78ed4beb4de7ceae3a809262d4ce242394342ccd1d58cef7d49dbc2def46")
    english_reference = "MISTER QUILTER IS THE APOSTLE OF THE MIDDLE CLASSES AND WE ARE GLAD TO WELCOME HIS GOSPEL"
    transcriber = Transcriber()
    transcriber.load()
    report = {"scope": "One public read-speech clip per RU/EN; synthetic RU+EN concatenation; not natural multilingual conversation or hardware capture", "kazakh_tested": False, "physical_capture_tested": False,
              "english_source": "https://huggingface.co/datasets/hf-internal-testing/librispeech_asr_dummy/tree/5be91486e11a2d616f4ec5db8d3fd248585ac07a",
              "english_license": "CC BY 4.0, LibriSpeech/OpenSLR12, Vassil Panayotov et al.; underlying LibriVox books public domain",
              "samples": []}
    for language, audio, reference in (("ru", russian, EXPECTED_TEXT), ("en", english, english_reference),
                                     ("synthetic_ru_en", np.concatenate((russian, np.zeros(5600, dtype=np.float32), english)), EXPECTED_TEXT + " " + english_reference)):
        started = time.perf_counter()
        words = transcriber.transcribe_words(audio, 16000)
        hypothesis = " ".join(word.text for word in words)
        ref, hyp = normalize_raw_text(reference).split(), normalize_raw_text(hypothesis).split()
        edits = distance(ref, hyp)
        report["samples"].append({"language": language, "reference": reference, "hypothesis": hypothesis,
            "reference_words": len(ref), "word_edits": edits, "wer": round(edits / len(ref), 4),
            "duration_seconds": len(audio) / 16000, "inference_seconds": round(time.perf_counter() - started, 3)})
    # A separate real eight-second pipeline check, still generated public PCM;
    # it does not certify natural code-switching or simultaneous human voices.
    mixed = np.concatenate((russian, np.zeros(5600, dtype=np.float32), english))
    segments, statuses = [], []
    processor = StreamProcessor(transcriber, segments.append, lambda kind, data: statuses.append((kind, data)))
    processor.start()
    for start in range(0, len(mixed), 1600):
        assert processor.feed(AudioBlock("microphone", mixed[start:start + 1600], 16000, start / 16000))
    processor.stop()
    hypothesis = " ".join(s.text for s in sorted(segments, key=lambda s: s.start))
    ref = normalize_raw_text(EXPECTED_TEXT + " " + english_reference).split()
    edits = distance(ref, normalize_raw_text(hypothesis).split())
    report["synthetic_mixed_streaming"] = {"hypothesis": hypothesis, "word_edits": edits, "reference_words": len(ref),
        "wer": round(edits / len(ref), 4), "pending_after_stop": processor.pending_seconds,
        "warnings": [kind for kind, _ in statuses if kind != "progress"]}
    report["blocked_python_network_attempts"] = blocked_attempts()
    with args.report.open("x", encoding="utf-8") as out:
        json.dump(report, out, indent=2, ensure_ascii=False)


if __name__ == "__main__":
    main()
