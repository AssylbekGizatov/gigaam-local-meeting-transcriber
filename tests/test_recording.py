"""Synthetic PCM checks for explicit optional separate WAV recording."""
import json
import wave

import numpy as np

from gigaam_meeting.contracts import AudioBlock
from gigaam_meeting.recording import AudioRecorder


def block(source="microphone", start=0, rate=16000, channels=1, duration=0.1, value=0.125, discontinuity=False):
    return AudioBlock(source, np.full((round(rate * duration), channels), value, dtype=np.float32), rate, start, discontinuity)


def timeline(recorder):
    return json.loads((recorder.folder / "timeline.json").read_text(encoding="utf-8"))


def test_opt_in_wav_sources_remain_separate_and_on_shared_clock(tmp_path):
    errors = []
    recorder = AudioRecorder(tmp_path / "explicit", errors.append)
    recorder.feed(block("microphone", 0.1, 16000, 1))
    recorder.feed(block("system", 0.15, 48000, 2))
    recorder.stop()
    manifest = timeline(recorder)
    assert not errors and manifest["complete"]
    assert len(manifest["files"]) == 2
    entries = {entry["source"]: entry for entry in manifest["files"]}
    for source, rate, channels, start in [("microphone", 16000, 1, 0.1), ("system", 48000, 2, 0.15)]:
        entry = entries[source]
        assert entry["start"] == start and entry["sample_rate"] == rate and entry["channels"] == channels
        with wave.open(str(recorder.folder / entry["file"]), "rb") as wav:
            assert wav.getframerate() == rate and wav.getnchannels() == channels
            assert wav.getsampwidth() == 2 and wav.getnframes() == round(rate * 0.1)
            pcm = np.frombuffer(wav.readframes(wav.getnframes()), dtype="<i2")
            assert np.max(pcm) == round(32767 * 0.125)
    assert "session monotonic" in manifest["clock"]
    assert all("text" not in entry for entry in manifest["files"])


def test_pause_gap_and_device_rate_change_rotate_files_keep_real_time(tmp_path):
    recorder = AudioRecorder(tmp_path, lambda message: None)
    recorder.feed(block(start=0))
    recorder.feed(block(start=0.1))
    recorder.feed(block(start=5.0, discontinuity=True))
    recorder.feed(block(start=5.1, rate=48000, channels=2))
    recorder.stop()
    entries = timeline(recorder)["files"]
    assert len(entries) == 3
    assert entries[0]["frames"] == 3200
    assert [entry["start"] for entry in entries] == [0, 5.0, 5.1]
    assert [entry["sample_rate"] for entry in entries] == [16000, 16000, 48000]
    for entry in entries:
        with wave.open(str(recorder.folder / entry["file"]), "rb") as wav:
            assert wav.getnframes() == entry["frames"]


def test_new_recorders_never_overwrite_existing_sessions(tmp_path):
    first = AudioRecorder(tmp_path, lambda message: None)
    first.feed(block())
    first.stop()
    before = {file.name: file.read_bytes() for file in first.folder.iterdir()}
    second = AudioRecorder(tmp_path, lambda message: None)
    second.feed(block(value=0.25))
    second.stop()
    assert first.folder != second.folder
    assert before == {file.name: file.read_bytes() for file in first.folder.iterdir()}


def test_write_failure_is_reported_once_with_incomplete_manifest(tmp_path, monkeypatch):
    errors = []
    recorder = AudioRecorder(tmp_path, errors.append)

    def fail_write(block):
        raise OSError("simulated disk failure — no conversation content")

    monkeypatch.setattr(recorder, "_write", fail_write)
    recorder.feed(block())
    recorder.feed(block(start=0.1))
    recorder.stop()
    assert len(errors) == 1 and "Не удалось сохранить" in errors[0]
    assert "simulated" not in errors[0]
    assert timeline(recorder)["complete"] is False


def test_queue_overflow_is_explicit_and_memory_bounded(tmp_path, monkeypatch):
    from threading import Event
    entered, release = Event(), Event()
    errors = []
    recorder = AudioRecorder(tmp_path, errors.append)

    def blocked_write(block):
        entered.set()
        assert release.wait(3)

    monkeypatch.setattr(recorder, "_write", blocked_write)
    recorder.feed(block())
    assert entered.wait(1)
    for i in range(40):
        recorder.feed(block(start=i * 0.1))
    assert recorder._queue.qsize() <= 32
    assert len(errors) == 1
    release.set()
    recorder.stop()
    assert timeline(recorder)["complete"] is False


def test_wav_header_close_failure_is_reported_safely(tmp_path, monkeypatch):
    errors = []
    recorder = AudioRecorder(tmp_path, errors.append)

    class BrokenWriter:
        def close(self):
            raise OSError("simulated header disk-full failure")

    def create_broken_file(block):
        entry = {"file": "fake.wav", "source": "microphone", "start": 0, "frames": 0, "sample_rate": 16000, "channels": 1}
        recorder._files["microphone"] = (BrokenWriter(), entry)
        recorder._entries.append(entry)

    monkeypatch.setattr(recorder, "_write", create_broken_file)
    recorder.feed(block())
    recorder.stop()
    assert len(errors) == 1, "WAV header failure must not escape the writer thread silently"
    assert "simulated" not in errors[0]
    assert timeline(recorder)["complete"] is False
