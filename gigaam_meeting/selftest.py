"""Offline packaged-app checks with an explicitly public audio fixture.

This never opens capture devices. Generated Unicode export content is a format
test, not evidence of multilingual ASR quality. No user audio is accepted here.
"""
from __future__ import annotations

import hashlib
from importlib import metadata
import json
import math
from pathlib import Path
import platform
import sys
import time
import wave
import zipfile

import numpy as np

from .contracts import AudioBlock, Segment
from .engine import CODE_REVISION, MODEL_FILES, MODEL_REVISION, Transcriber, verify_model
from .exports import EXPORT_FORMATS, export_transcript
from .streaming import StreamProcessor

FIXTURE_SHA256 = "d8aaaa18a5098d7c6de0595ae7ac1e64cacd0d4022af3595213bdaf23be77e69"
FIXTURE_BYTES = 361324
FIXTURE_RELATIVE_PATH = Path("assets/asr-smoke.wav")
FIXTURE_SOURCE = "https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/example.wav"
EXPECTED_TEXT = "ничьих не требуя похвал счастлив уж я надеждой сладкой что дева с трепетом любви посмотрит может быть украдкой на песни грешные мои у лукоморья дуб зеленый"
SYNTHETIC_TEXT = "Бүгін қазақша сөйлейміз. Сегодня проверяем речь. English speech. ә ғ қ ң ө ұ ү һ і. Да да."


def read_fixture(root: Path) -> tuple[np.ndarray, int]:
    """Verify exact public WAV bytes before decoding its bounded local PCM."""
    path = root / FIXTURE_RELATIVE_PATH
    if path.stat().st_size != FIXTURE_BYTES:
        raise ValueError("fixture_size_mismatch")
    if hashlib.sha256(path.read_bytes()).hexdigest() != FIXTURE_SHA256:
        raise ValueError("fixture_checksum_mismatch")
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getnframes()) != (1, 2, 16000, 180640):
            raise ValueError("fixture_format_mismatch")
        samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2").astype(np.float32)
    samples *= np.float32(1 / 32768)
    return samples, 16000


def _runtime_info() -> dict:
    versions = {}
    for name in ("numpy", "torch", "torchaudio", "gigaam", "PyAudioWPatch", "python-docx", "reportlab"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "unavailable"
    return {"python": platform.python_version(), "platform": platform.system(),
            "architecture": platform.machine(), "frozen": bool(getattr(sys, "frozen", False)),
            "dependencies": versions}


def _model_info(root: Path) -> dict:
    directory = root / "models/gigaam"
    verify_model(directory)
    checksums = {}
    for name in MODEL_FILES:
        with (directory / name).open("rb") as stream:
            checksums[name] = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"name": "ai-sage/GigaAM-Multilingual", "architecture": "220M CTC",
            "device": "cpu", "dtype": "float32", "model_revision": MODEL_REVISION,
            "code_revision": CODE_REVISION, "sha256": checksums}


def _public_inference(root: Path) -> dict:
    samples, rate = read_fixture(root)
    transcriber = Transcriber(root / "models/gigaam")
    started = time.perf_counter()
    words = transcriber.transcribe_words(samples, rate)
    elapsed = time.perf_counter() - started
    text = " ".join(word.text for word in words)
    if text != EXPECTED_TEXT:
        raise ValueError("public_fixture_transcript_mismatch")
    duration = len(samples) / rate
    previous_start = -1.0
    for word in words:
        if (not math.isfinite(word.start) or not math.isfinite(word.end)
                or word.start < previous_start or word.start < 0 or word.end < word.start
                or word.end > duration + .01):
            raise ValueError("public_fixture_timestamp_mismatch")
        previous_start = word.start
    if not words:
        raise ValueError("public_fixture_empty_transcript")
    return {"fixture_sha256": FIXTURE_SHA256, "fixture_source": FIXTURE_SOURCE,
            "duration_seconds": duration, "inference_seconds_including_load": round(elapsed, 3),
            "recognized_text": text, "expected_text_matches": True,
            "word_count": len(words), "word_timestamps_valid": True,
            "scope": "One public Russian fixture; Kazakh/English/mixed ASR and physical capture are not verified by this check."}


class _CounterModel:
    """Synthetic marker model, deliberately no learned multilingual claims."""
    def __init__(self):
        self.calls = 0
        self.active = 0
        self.peak = 0

    def transcribe_words(self, samples, rate):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.calls += 1
        self.active -= 1
        return []


def _bounded_streaming() -> dict:
    model = _CounterModel()
    statuses = []
    processor = StreamProcessor(model, lambda segment: None,
                                lambda kind, data: statuses.append(kind) if kind != "progress" else None)
    processor.start()
    pcm = np.full(2 * 16000, .01, dtype=np.float32)
    peak_pending = 0.0
    try:
        # Simulate two hours of continuous audio, advancing the sample clock
        # rapidly while draining fake inference. No audio history is retained.
        for index in range(3600):
            if not processor.feed(AudioBlock("microphone", pcm, 16000, index * 2.0)):
                raise ValueError("synthetic_stream_unexpected_drop")
            peak_pending = max(peak_pending, processor.pending_seconds)
            deadline = time.monotonic() + 2
            while processor.pending_seconds > 10:
                if time.monotonic() > deadline:
                    raise TimeoutError("synthetic_stream_drain_timeout")
                time.sleep(.001)
        processor.stop()
        if processor.pending_seconds != 0 or peak_pending > processor.max_pending_seconds or model.peak != 1 or statuses:
            raise ValueError("synthetic_stream_bounds_failed")
    finally:
        processor.stop()
    return {"scope": "Fake model, no language quality assertion", "simulated_seconds": 7200,
            "inference_windows": model.calls, "maximum_pending_seconds": round(peak_pending, 3),
            "configured_bound_seconds": processor.max_pending_seconds,
            "peak_parallel_inference": model.peak, "pending_after_stop_seconds": processor.pending_seconds}


def _synthetic_exports(folder: Path) -> dict:
    folder.mkdir(parents=True, exist_ok=False)
    segments = [Segment("system", 6, 8, SYNTHETIC_TEXT),
                Segment("microphone", 1, 2, "Микрофон: да да")]
    files = {}
    for kind in EXPORT_FORMATS:
        destination = export_transcript(segments, folder / ("synthetic-unicode." + kind.lower()), kind)
        content = destination.read_bytes()
        if kind in {"TXT", "MD"}:
            text = content.decode("utf-8")
            if "ә ғ қ ң ө ұ ү һ і" not in text or "да да" not in text or text.index("00:00:01.000") > text.index("00:00:06.000"):
                raise ValueError("synthetic_unicode_text_export_mismatch")
        elif kind == "DOCX":
            with zipfile.ZipFile(destination) as archive:
                xml = archive.read("word/document.xml").decode("utf-8")
            if SYNTHETIC_TEXT not in xml or "Микрофон" not in xml:
                raise ValueError("synthetic_unicode_docx_export_mismatch")
        elif not content.startswith(b"%PDF-") or b"/FontFile2" not in content:
            raise ValueError("synthetic_pdf_embedded_font_missing")
        try:
            export_transcript(segments, destination, kind)
        except FileExistsError:
            pass
        else:
            raise ValueError("export_overwrite_not_blocked")
        if destination.read_bytes() != content:
            raise ValueError("existing_export_changed")
        files[kind] = {"filename": destination.name, "bytes": len(content),
                       "sha256": hashlib.sha256(content).hexdigest(), "existing_file_preserved": True}
    return {"scope": "Synthetic Unicode and repeated-word export checks only; not model language tests.",
            "source_labels": ["microphone", "system"], "files": files}


def run_self_test(bundle_root: Path | str, report_path: Path | str) -> dict:
    """Verify extracted/source assets and run genuine offline public inference.

    JSON and generated-format artifacts are explicit local test outputs. An
    existing report or artifact folder is never overwritten. Caller's runtime
    network guard remains responsible for broader in-process isolation.
    """
    from .storage import validate_local_path
    from .offline import blocked_attempts
    root = validate_local_path(Path(bundle_root).absolute())
    destination = validate_local_path(Path(report_path).absolute())
    attempts_before = blocked_attempts()
    report = {"schema_version": 1, "status": "running", "runtime": _runtime_info(),
              "checks": [], "physical_capture_tested": False,
              "real_kazakh_english_mixed_speech_tested": False,
              "network_scope": "Pinned local native model; no network request or hardware capture in this test. OS-level network isolation is not certified."}
    if destination.exists():
        report.update(status="failed", error={"stage": "report", "code": "report_already_exists"})
        return report
    stage = "model"
    started = time.perf_counter()
    try:
        report["model"] = _model_info(root)
        report["checks"].append({"name": "official-model-checksums", "status": "passed"})
        stage = "public-fixture-inference"
        report["fixture"] = _public_inference(root)
        report["checks"].append({"name": stage, "status": "passed"})
        stage = "bounded-streaming-fake-model"
        report["streaming"] = _bounded_streaming()
        report["checks"].append({"name": stage, "status": "passed"})
        stage = "synthetic-unicode-exports"
        report["exports"] = _synthetic_exports(destination.parent / (destination.stem + "-artifacts"))
        report["checks"].append({"name": stage, "status": "passed"})
        report["status"] = "passed"
    except Exception as error:
        # No raw dependency message or traceback: those can include local paths
        # or input contents. This test uses only pinned public/generated input.
        report["status"] = "failed"
        report["error"] = {"stage": stage, "type": type(error).__name__}
        safe_codes = {"fixture_size_mismatch", "fixture_checksum_mismatch", "fixture_format_mismatch",
                      "public_fixture_transcript_mismatch", "public_fixture_timestamp_mismatch",
                      "public_fixture_empty_transcript", "synthetic_stream_unexpected_drop",
                      "synthetic_stream_drain_timeout", "synthetic_stream_bounds_failed",
                      "synthetic_unicode_text_export_mismatch", "synthetic_unicode_docx_export_mismatch",
                      "synthetic_pdf_embedded_font_missing", "export_overwrite_not_blocked", "existing_export_changed"}
        if len(error.args) == 1 and isinstance(error.args[0], str) and error.args[0] in safe_codes:
            report["error"]["code"] = error.args[0]
        report["checks"].append({"name": stage, "status": "failed"})
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    report["blocked_python_network_attempts"] = blocked_attempts() - attempts_before
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return report
