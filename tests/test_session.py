"""Lifecycle tests use fake sources only; never open a user's audio devices."""
from queue import Empty, Queue
from threading import Event as ThreadEvent
from time import monotonic, sleep

import numpy as np
import pytest

from gigaam_meeting.audio import AudioCaptureError
from gigaam_meeting.contracts import AudioBlock, AudioDevice, Segment, SessionConfig, Word
from gigaam_meeting.engine import EngineError
from gigaam_meeting.session import SessionManager
import gigaam_meeting.session as session_module


MIC = AudioDevice(1, "Fake microphone", "microphone", 16000, 1, fingerprint="mic")
SYSTEM = AudioDevice(2, "Fake headphones", "system", 48000, 2, fingerprint="system")


@pytest.mark.parametrize("enabled", [False, True])
def test_session_echo_edits_and_toggle_keep_capture_pcm_and_tail(harness, monkeypatch, enabled):
    from dataclasses import replace
    from gigaam_meeting.echo import EchoAudioHistory
    manager, _, captures, processors, events, config, _ = harness
    # Acoustic detector has separate real/synthetic PCM checks in test_echo.
    # This test establishes end-to-end session/event/stop routing independently.
    monkeypatch.setattr(EchoAudioHistory, "matches", lambda *args: True)
    manager.start(replace(config, reduce_echo=enabled))
    wait_until(lambda: manager.state == "recording")
    values = np.linspace(-.2, .2, 640, dtype=np.float32)
    original = values.copy()
    captured = AudioBlock("microphone", values, 16000, 0)
    captures[-1].on_block(captured)
    assert processors[-1].blocks[-1] is captured and np.array_equal(values, original)
    words = tuple(Word(1 + i * .3, 1.2 + i * .3, token) for i, token in enumerate(["today", "we", "test"]))
    manager._segment(Segment("microphone", 1, 1.8, "today we test", words))
    manager._segment(Segment("system", 1, 1.8, "today we test", words))
    spoken = [s for s in manager.segments if s.text == "today we test"]
    assert [s.source for s in spoken] == (["system"] if enabled else ["microphone", "system"])
    delivered = events_from(events)
    assert any(e.kind == "echo_update" for e in delivered) == enabled
    if enabled:
        history = manager._echo
    manager.stop()
    wait_until(lambda: manager.state == "idle")
    assert any(s.text == "fake recognition" for s in manager.segments)
    assert manager._echo is None
    if enabled:
        assert not history._microphone and not history._system
        assert all(not a.any() for a in history.audio._values.values())


def wait_until(condition, timeout=3):
    deadline = monotonic() + timeout
    while not condition():
        if monotonic() > deadline:
            raise AssertionError("Timed out waiting for the deterministic fake lifecycle")
        sleep(0.005)


def events_from(events):
    items = []
    while True:
        try:
            items.append(events.get_nowait())
        except Empty:
            return items


class FakeModel:
    def __init__(self):
        self.load_entered = ThreadEvent()
        self.load_release = ThreadEvent()
        self.load_release.set()
        self.load_calls = 0
        self.load_error = None

    def load(self):
        self.load_calls += 1
        self.load_entered.set()
        assert self.load_release.wait(3)
        if self.load_error:
            raise self.load_error


class FakeProcessor:
    def __init__(self, model, on_segment, on_status):
        self.model, self.on_segment, self.on_status = model, on_segment, on_status
        self.calls = []
        self.blocks = []
        self.pending_seconds = 2.0
        self.stopped = False

    def start(self):
        self.calls.append("start")

    def feed(self, block):
        self.calls.append("feed")
        self.blocks.append(block)

    def pause_flush(self):
        self.calls.append("pause_flush")

    def stop(self):
        self.calls.append("stop")
        self.stopped = True
        # This tail must be visible before finished; no early success event.
        self.on_segment(Segment("microphone", 0, 1, "fake recognition"))


class FakeCapture:
    def __init__(self, devices, on_block, on_error, on_level, origin):
        self.devices, self.on_block, self.on_error, self.on_level = devices, on_block, on_error, on_level
        self.origin = origin
        self.started = self.stopped = False
        self.paused = False
        self.resume_error = None
        self.stop_error = None
        self.tail_on_pause = None
        self.tail_on_stop = None

    def start(self):
        self.started = True

    def pause(self):
        self.paused = True
        if self.tail_on_pause:
            self.on_block(self.tail_on_pause)

    def resume(self):
        if self.resume_error:
            raise self.resume_error
        self.paused = False

    def stop(self):
        self.stopped = True
        if self.tail_on_stop:
            self.on_block(self.tail_on_stop)
        if self.stop_error:
            raise self.stop_error


@pytest.fixture
def harness(monkeypatch, tmp_path):
    model = FakeModel()
    captures, processors, model_factories = [], [], []

    def make_model():
        model_factories.append(True)
        return model

    def make_capture(*args):
        capture = FakeCapture(*args)
        captures.append(capture)
        return capture

    def make_processor(*args):
        processor = FakeProcessor(*args)
        processors.append(processor)
        return processor

    monkeypatch.setattr(session_module, "StreamProcessor", make_processor)
    events = Queue()
    manager = SessionManager(events, transcriber_factory=make_model,
        capture_factory=make_capture, device_provider=lambda: {"microphone": [MIC], "system": [SYSTEM]})
    config = SessionConfig(MIC, SYSTEM, True, tmp_path / "explicit-local-folder", False)
    yield manager, model, captures, processors, events, config, model_factories
    model.load_release.set()
    if manager.state not in {"idle", "error"}:
        manager.stop()
        wait_until(lambda: manager.state in {"idle", "error"})
    manager.close()


@pytest.mark.parametrize("config_change", ["no_consent", "no_sources", "wrong_source"])
def test_no_capture_or_model_without_valid_explicit_choice(harness, config_change):
    manager, _, captures, processors, _, config, model_factories = harness
    invalid = {
        "no_consent": SessionConfig(MIC, SYSTEM, False, config.output_folder),
        "no_sources": SessionConfig(None, None, True, config.output_folder),
        "wrong_source": SessionConfig(SYSTEM, None, True, config.output_folder),
    }[config_change]
    with pytest.raises(AudioCaptureError):
        manager.start(invalid)
    with pytest.raises(AudioCaptureError):
        manager.preview(invalid)
    assert not captures and not processors and not model_factories
    assert manager.state == "idle"


def test_stop_during_model_load_never_opens_capture_and_is_nonblocking(harness):
    manager, model, captures, processors, events, config, _ = harness
    model.load_release.clear()
    before = monotonic()
    manager.start(config)
    assert monotonic() - before < 0.2
    assert model.load_entered.wait(1)
    before = monotonic()
    manager.stop()
    assert monotonic() - before < 0.2
    model.load_release.set()
    wait_until(lambda: manager.state == "idle")
    assert not captures and not processors
    assert not config.output_folder.exists()
    assert [event.kind for event in events_from(events)].count("finished") == 1


def test_close_during_model_load_never_captures_and_rejects_new_sessions(harness):
    manager, model, captures, processors, events, config, _ = harness
    model.load_release.clear()
    manager.start(config)
    assert model.load_entered.wait(1)
    before = monotonic()
    manager.close()
    assert monotonic() - before < 0.2
    model.load_release.set()
    wait_until(lambda: manager.state == "idle")
    assert not captures and not processors
    with pytest.raises(AudioCaptureError):
        manager.start(config)
    assert any(event.kind == "finished" for event in events_from(events))


@pytest.mark.parametrize("action", ["start", "preview"])
def test_stop_during_stalled_driver_start_remains_nonblocking(harness, action):
    manager, _, captures, _, events, config, _ = harness
    entered, release = ThreadEvent(), ThreadEvent()
    original_factory = manager._capture_factory

    def stalled_factory(*args):
        capture = original_factory(*args)

        def start_driver():
            entered.set()
            assert release.wait(3)
            capture.started = True

        capture.start = start_driver
        return capture

    manager._capture_factory = stalled_factory
    try:
        getattr(manager, action)(config)
        assert entered.wait(1)
        before = monotonic()
        manager.stop()
        assert monotonic() - before < 0.2, "A blocked audio driver must not hold the UI stop action"
    finally:
        release.set()
    wait_until(lambda: manager.state == "idle")
    assert captures[0].stopped
    received = events_from(events)
    assert not any(event.kind == "state" and event.data.get("state") == "recording" for event in received)
    states = [event.data.get("state") for event in received if event.kind == "state"]
    assert states.index("stopping") < states.index("draining") < states.index("idle")


def test_preview_uses_no_model_no_recording_and_stops_promptly(harness):
    manager, _, captures, processors, events, config, model_factories = harness
    recording_config = SessionConfig(MIC, SYSTEM, True, config.output_folder, True)
    manager.preview(recording_config, seconds=3)
    wait_until(lambda: bool(captures) and captures[0].started)
    manager.stop()
    wait_until(lambda: manager.state == "idle")
    assert captures[0].stopped
    assert not processors and not model_factories
    assert not config.output_folder.exists()
    assert [event.kind for event in events_from(events)].count("finished") == 1


def test_short_preview_finishes_without_any_autorecord(harness):
    manager, _, captures, _, events, config, model_factories = harness
    manager.preview(config, seconds=0.1)
    wait_until(lambda: manager.state == "idle")
    assert captures[0].started and captures[0].stopped
    assert not model_factories
    sleep(0.03)
    assert manager.state == "idle" and len(captures) == 1
    assert any(event.kind == "finished" for event in events_from(events))


def test_pause_drains_owned_capture_blocks_before_flush_and_resume(harness):
    manager, _, captures, processors, _, config, _ = harness
    manager.start(config)
    wait_until(lambda: manager.state == "recording")
    block = AudioBlock("microphone", np.ones((160, 1), dtype=np.float32) * 0.1, 16000, 0)
    captures[0].tail_on_pause = block
    manager.pause()
    wait_until(lambda: manager.state == "paused")
    assert processors[0].calls == ["start", "feed", "pause_flush"]
    assert processors[0].blocks == [block]
    manager.resume()
    wait_until(lambda: manager.state == "recording")
    assert not captures[0].paused and len(captures) == 1


def test_stop_during_stalled_resume_does_not_publish_recording_again(harness):
    manager, _, captures, _, events, config, _ = harness
    manager.start(config)
    wait_until(lambda: manager.state == "recording")
    manager.pause()
    wait_until(lambda: manager.state == "paused")
    events_from(events)
    entered, release = ThreadEvent(), ThreadEvent()

    def resume_driver():
        entered.set()
        assert release.wait(3)
        captures[0].paused = False

    captures[0].resume = resume_driver
    try:
        manager.resume()
        assert entered.wait(1)
        before = monotonic()
        manager.stop()
        assert monotonic() - before < 0.2
    finally:
        release.set()
    wait_until(lambda: manager.state == "idle")
    assert captures[0].stopped
    assert not any(event.kind == "state" and event.data.get("state") == "recording" for event in events_from(events))


def test_final_capture_tail_and_final_transcript_precede_finished(harness):
    manager, _, captures, processors, events, config, _ = harness
    manager.start(config)
    wait_until(lambda: manager.state == "recording")
    block = AudioBlock("system", np.ones((480, 2), dtype=np.float32) * 0.2, 48000, 0.02)
    captures[0].tail_on_stop = block
    manager.stop()
    manager.stop()
    wait_until(lambda: manager.state == "idle")
    assert processors[0].calls == ["start", "feed", "stop"]
    received = events_from(events)
    kinds = [event.kind for event in received]
    assert kinds.index("segment") < kinds.index("finished")
    assert kinds.count("finished") == 1
    assert not config.output_folder.exists()  # Recording disabled creates no files or directories.


def test_recording_requires_opt_in_and_finishes_separate_wav_before_success(harness):
    import json
    import wave
    manager, _, captures, _, events, config, _ = harness
    enabled = SessionConfig(MIC, SYSTEM, True, config.output_folder, True)
    manager.start(enabled)
    wait_until(lambda: manager.state == "recording")
    captures[0].on_block(AudioBlock("microphone", np.ones((160, 1), dtype=np.float32) * 0.1, 16000, 0.05))
    captures[0].on_block(AudioBlock("system", np.ones((480, 2), dtype=np.float32) * 0.2, 48000, 0.1))
    manager.stop()
    wait_until(lambda: manager.state == "idle")
    recordings = list(config.output_folder.glob("audio-*"))
    assert len(recordings) == 1
    manifest = json.loads((recordings[0] / "timeline.json").read_text(encoding="utf-8"))
    assert manifest["complete"] and {file["source"] for file in manifest["files"]} == {"microphone", "system"}
    for file in manifest["files"]:
        with wave.open(str(recordings[0] / file["file"]), "rb") as wav:
            assert wav.getnframes() == file["frames"]
    assert any(event.kind == "finished" for event in events_from(events))


def test_device_fault_stops_all_sources_and_does_not_silently_restart(harness):
    manager, _, captures, processors, events, config, _ = harness
    manager.start(config)
    wait_until(lambda: manager.state == "recording")
    captures[0].on_error("Выбранное устройство отключено; выберите его заново.")
    wait_until(lambda: manager.state == "error")
    assert captures[0].stopped and processors[0].stopped
    errors = [event for event in events_from(events) if event.kind == "error"]
    assert len(errors) == 1 and "отключено" in errors[0].message
    sleep(0.03)
    assert len(captures) == 1 and manager.state == "error"


def test_resume_device_change_stops_instead_of_fallback(harness):
    manager, _, captures, processors, events, config, _ = harness
    manager.start(config)
    wait_until(lambda: manager.state == "recording")
    manager.pause()
    wait_until(lambda: manager.state == "paused")
    captures[0].resume_error = AudioCaptureError("Устройство изменилось; автоматической замены нет.")
    manager.resume()
    wait_until(lambda: manager.state == "error")
    assert captures[0].stopped and processors[0].stopped and len(captures) == 1
    assert any("замены нет" in event.message for event in events_from(events) if event.kind == "error")


def test_model_failure_opens_no_sources_and_exposes_safe_error(harness):
    manager, model, captures, _, events, config, _ = harness
    model.load_error = EngineError("Модель не найдена. Распакуйте папку models.")
    manager.start(config)
    wait_until(lambda: manager.state == "error")
    assert not captures and not config.output_folder.exists()
    assert any("Модель не найдена" in event.message for event in events_from(events) if event.kind == "error")


def test_each_restart_requires_new_start_and_reuses_loaded_model(harness):
    manager, model, captures, _, _, config, factories = harness
    manager.start(config)
    wait_until(lambda: manager.state == "recording")
    manager.stop()
    wait_until(lambda: manager.state == "idle")
    sleep(0.03)
    assert len(captures) == 1
    with pytest.raises(AudioCaptureError):
        manager.start(SessionConfig(MIC, SYSTEM, False, config.output_folder))
    manager.start(config)
    wait_until(lambda: manager.state == "recording")
    assert len(captures) == 2 and len(factories) == 1 and model.load_calls == 2


def test_cleanup_continues_when_driver_stop_raises(harness):
    manager, _, captures, processors, events, config, _ = harness
    manager.start(config)
    wait_until(lambda: manager.state == "recording")
    captures[0].stop_error = OSError("simulated driver stop failure")
    manager.stop()
    wait_until(lambda: manager.state == "error")
    assert processors[0].stopped, "A failed driver close must not leak the inference worker"
    assert any(event.kind == "error" for event in events_from(events))


def test_overload_notice_contains_no_recognized_content_and_is_visible_event(harness):
    manager, _, _, _, events, _, _ = harness
    manager._status("overload", {"message": "Распознавание отстаёт; часть блоков пропущена.", "pending_seconds": 48})
    warning = events_from(events)[0]
    assert warning.kind == "warning" and "пропущена" in warning.message
