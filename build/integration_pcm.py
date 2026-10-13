"""Real GigaAM -> eight-second stream integration on public PCM only.

Two synthetic source labels replay the same pinned WAV, at 16/48 kHz. Hardware
is never opened. Input is accelerated, so this is not a real-time performance
or microphone/loopback quality claim. Outputs contain only public fixture text.
"""
from __future__ import annotations

from pathlib import Path
import sys
import argparse
import dataclasses
import hashlib
import json
import math
import platform
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Install before every third-party import, including NumPy/Torch/Torchaudio.
from gigaam_meeting.offline import blocked_attempts, install  # noqa: E402
install()


def run_integration(root: Path, report_path: Path) -> dict:
    if report_path.exists():
        raise FileExistsError("report_exists")
    report = {"schema_version": 1, "status": "running", "runtime": {"python": platform.python_version(), "frozen": False},
              "scope": "Real native CPU GigaAM, accelerated public PCM at two sample rates and two synthetic source labels",
              "hardware_capture_tested": False, "real_kazakh_english_mixed_tested": False,
              "real_time_performance_tested": False, "checks": []}
    report["reviewed_source_sha256"] = {name: hashlib.sha256((root / "gigaam_meeting" / name).read_bytes()).hexdigest()
                                        for name in ("engine.py", "streaming.py", "selftest.py", "offline.py")}
    processor = None
    stage = "dependency-imports"
    started = time.perf_counter()
    def check(name, passed):
        report["checks"].append({"name": name, "status": "passed" if passed else "failed"})
    try:
        from gigaam_meeting.contracts import AudioBlock
        from gigaam_meeting.engine import Transcriber
        from gigaam_meeting.selftest import EXPECTED_TEXT, FIXTURE_SHA256, _model_info, _runtime_info, read_fixture
        from gigaam_meeting.streaming import StreamProcessor
        report["runtime"] = _runtime_info()
        stage = "model-checksums"
        report["model"] = _model_info(root)
        check(stage, True)
        stage = "fixture-resampling"
        samples, rate = read_fixture(root)
        import torch
        import torchaudio.functional as audio
        high_rate = 48000
        resampled = audio.resample(torch.from_numpy(samples.copy()), rate, high_rate).numpy()
        duration = len(samples) / rate
        source_data = {"microphone": (samples, rate), "system": (resampled, high_rate)}
        report["fixture"] = {"sha256": FIXTURE_SHA256, "duration_seconds": duration,
                             "source_rates": {source: hz for source, (_, hz) in source_data.items()},
                             "resampling": "torchaudio.functional.resample 16000->48000; engine resamples native input back to 16000"}
        check(stage, len(resampled) == 3 * len(samples))
        stage = "model-load"
        transcriber = Transcriber(root / "models/gigaam")
        load_started = time.perf_counter()
        transcriber.load()
        report["initial_model_load_seconds"] = round(time.perf_counter() - load_started, 3)
        check(stage, True)
        stage = "direct-baseline"
        report["direct_baselines"] = {}
        for source, (pcm, hz) in source_data.items():
            begin = time.perf_counter()
            words = transcriber.transcribe_words(pcm, hz)
            text = " ".join(word.text for word in words)
            report["direct_baselines"][source] = {"recognized_text": text, "word_count": len(words),
                                                  "expected_text_matches": text == EXPECTED_TEXT,
                                                  "seconds": round(time.perf_counter() - begin, 3)}
            check("direct-baseline-" + source, text == EXPECTED_TEXT)
        stage = "real-eight-second-stream"
        segments = []
        statuses = []
        progress = []
        windows = []
        class PublicWindowTrace:
            def transcribe_words(self, pcm, hz):
                words = transcriber.transcribe_words(pcm, hz)
                windows.append({"sample_rate": hz, "duration_seconds": len(pcm) / hz,
                                "relative_words": [dataclasses.asdict(word) for word in words]})
                return words
        def observe_status(kind, data):
            (progress if kind == "progress" else statuses).append({"kind": kind, **data})
        processor = StreamProcessor(PublicWindowTrace(), segments.append, observe_status)
        processor.start()
        timeline_start = 12.0
        peak_pending = 0.0
        rejected_blocks = 0
        stream_started = time.perf_counter()
        # Interleaved 100ms blocks, on one explicit session clock. Native
        # sample positions preserve fractional final frames and both rates.
        for index in range(math.ceil(duration * 10)):
            for source, (pcm, hz) in source_data.items():
                first = index * (hz // 10)
                last = min((index + 1) * (hz // 10), len(pcm))
                if first >= last:
                    continue
                accepted = processor.feed(AudioBlock(source, pcm[first:last], hz, timeline_start + first / hz))
                rejected_blocks += int(not accepted)
                peak_pending = max(peak_pending, processor.pending_seconds)
        # No more callback blocks: exercise loopback's idle partial flush with
        # actual GigaAM. stop() below must not be what unlocks these last words.
        idle_deadline = time.monotonic() + 120
        while processor.pending_seconds and time.monotonic() < idle_deadline:
            time.sleep(.05)
        automatically_drained = processor.pending_seconds == 0
        check("partial-window-idle-flush-without-stop", automatically_drained)
        peak_pending = max(peak_pending, processor.pending_seconds)
        processor.stop()
        by_source = {source: sorted((segment for segment in segments if segment.source == source),
                                    key=lambda segment: (segment.start, segment.end)) for source in source_data}
        report["stream"] = {"window_seconds": 8, "overlap_seconds": 1,
                            "shared_timeline_start_seconds": timeline_start,
                            "input_duration_seconds_per_source": duration,
                            "maximum_pending_seconds": round(peak_pending, 3),
                            "configured_bound_seconds": processor.max_pending_seconds,
                            "pending_after_stop_seconds": processor.pending_seconds,
                            "rejected_blocks": rejected_blocks,
                            "status_events": statuses,
                            "progress_updates": len(progress),
                            "last_progress_pending_seconds": progress[-1]["pending_seconds"] if progress else None,
                            "processing_seconds": round(time.perf_counter() - stream_started, 3),
                            "real_model_windows": windows,
                            "sources": {}}
        for source, rows in by_source.items():
            text = " ".join(row.text for row in rows)
            valid_times = all(math.isfinite(row.start) and math.isfinite(row.end)
                              and timeline_start <= row.start <= row.end <= timeline_start + duration + .01 for row in rows)
            ordered_times = all(first.end <= second.start + .01 for first, second in zip(rows, rows[1:]))
            report["stream"]["sources"][source] = {"recognized_text": text,
                "expected_text_matches": text == EXPECTED_TEXT,
                "word_count": len(text.split()), "segments": [dataclasses.asdict(row) for row in rows],
                "shared_timeline_valid": valid_times and ordered_times}
            check("stream-exact-text-" + source, text == EXPECTED_TEXT)
            check("shared-timeline-" + source, valid_times and ordered_times and bool(rows))
        check("source-independence", bool(by_source["microphone"]) and bool(by_source["system"]))
        check("bounded-pending-drained", peak_pending <= processor.max_pending_seconds and processor.pending_seconds == 0)
        check("no-dropped-blocks-or-faults", rejected_blocks == 0 and not statuses)
        check("final-progress-without-callback", bool(progress) and progress[-1]["pending_seconds"] == 0)
        report["blocked_python_network_attempts"] = blocked_attempts()
        check("python-network-attempts", report["blocked_python_network_attempts"] == 0)
        report["status"] = "passed" if all(item["status"] == "passed" for item in report["checks"]) else "failed"
    except Exception as error:
        report["status"] = "failed"
        report["error"] = {"stage": stage, "type": type(error).__name__}
        check(stage, False)
    finally:
        if processor is not None:
            processor.stop()
    report["elapsed_seconds"] = round(time.perf_counter() - started, 3)
    report["network_limit"] = "Python guard only; OS-level network disconnection/native endpoint monitoring are not performed by this driver"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=False)
        stream.write("\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = run_integration(args.root.resolve(), args.report.resolve())
    except Exception as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 1
    # Paths and public speech are intentionally kept out of console logs.
    print(json.dumps({"status": result["status"], "elapsed_seconds": result["elapsed_seconds"],
                      "failed_checks": [item["name"] for item in result["checks"] if item["status"] == "failed"]}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
