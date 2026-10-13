from __future__ import annotations

import threading
import time
from dataclasses import replace

import numpy as np
import pytest

from gigaam_meeting import audio
from gigaam_meeting.streaming import StreamProcessor


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert predicate()


class Stream:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.active = False
        self.closed = False
        self.stops = 0

    def get_time(self):
        return 5000.0 if self.kwargs["input_device_index"] == 1 else 8000.0

    def start_stream(self):
        self.active = True

    def stop_stream(self):
        self.stops += 1
        self.active = False

    def close(self):
        self.closed = True

    def is_active(self):
        return self.active

    def emit(self, values, adc=None, status=0):
        pcm = np.asarray(values, dtype="<i2").reshape(-1, self.kwargs["channels"])
        result = self.kwargs["stream_callback"](pcm.tobytes(), len(pcm),
            {"input_buffer_adc_time": self.get_time() if adc is None else adc, "current_time": self.get_time()}, status)
        if result[1] == 2:  # PortAudio stops a stream when its callback aborts.
            self.active = False
        return result


class Factory:
    paWASAPI = 13
    paInt16 = 8
    paContinue = 0
    paAbort = 2

    def __init__(self):
        self.infos = {
            0: dict(index=0, name="MME microphone", hostApi=0, maxInputChannels=1, maxOutputChannels=0, defaultSampleRate=44100.0, isLoopbackDevice=False),
            1: dict(index=1, name="Chosen microphone", hostApi=2, maxInputChannels=1, maxOutputChannels=0, defaultSampleRate=44100.0, isLoopbackDevice=False),
            2: dict(index=2, name="Chosen speakers", hostApi=2, maxInputChannels=0, maxOutputChannels=2, defaultSampleRate=48000.0, isLoopbackDevice=False),
            3: dict(index=3, name="Chosen speakers [Loopback]", hostApi=2, maxInputChannels=2, maxOutputChannels=0, defaultSampleRate=48000.0, isLoopbackDevice=True),
        }
        self.managers = []
        self.streams = []
        self.default_input = 1
        self.unsupported = False

    def PyAudio(self):
        factory = self

        class Manager:
            terminated = False

            def get_host_api_info_by_type(self, kind):
                assert kind == factory.paWASAPI
                return dict(index=2, defaultInputDevice=factory.default_input, defaultOutputDevice=2)

            def get_default_wasapi_loopback(self):
                return factory.infos[3]

            def get_device_count(self):
                return len(factory.infos)

            def get_device_info_by_index(self, index):
                if index not in factory.infos:
                    raise OSError("private driver details")
                return factory.infos[index]

            def is_format_supported(self, rate, **kwargs):
                return not factory.unsupported

            def open(self, **kwargs):
                assert kwargs["start"] is False
                assert kwargs["input"] is True
                stream = Stream(kwargs)
                factory.streams.append(stream)
                return stream

            def terminate(self):
                self.terminated = True

        manager = Manager()
        self.managers.append(manager)
        return manager


@pytest.fixture
def fake(monkeypatch):
    factory = Factory()
    now = [100.0]
    monkeypatch.setattr(audio, "_backend", lambda: factory)
    monkeypatch.setattr(audio, "perf_counter", lambda: now[0])
    return factory, now


def group(fake, sources=("microphone", "system"), on_block=None):
    factory, _ = fake
    found = audio.list_devices()
    blocks, errors, levels = [], [], []
    capture = audio.CaptureGroup([found[source][0] for source in sources], on_block or blocks.append,
        errors.append, lambda source, value: levels.append((source, value)), 99.0)
    return capture, blocks, errors, levels


def test_list_only_wasapi_mic_and_true_loopback(fake):
    factory, _ = fake
    devices = audio.list_devices()
    assert [d.id for d in devices["microphone"]] == [1]
    assert [d.id for d in devices["system"]] == [3]
    assert all(d.is_default and d.fingerprint for values in devices.values() for d in values)
    assert factory.managers[-1].terminated
    assert audio.CAPTURE_SCOPE == "device" and not audio.SUPPORTS_PROCESS_CAPTURE


def test_native_rates_stereo_normalization_and_one_session_clock(fake):
    factory, _ = fake
    capture, blocks, errors, levels = group(fake)
    try:
        capture.start()
        mic, system = factory.streams
        assert mic.kwargs["rate"] == 44100 and system.kwargs["rate"] == 48000
        assert mic.emit([[-32768], [16384]], adc=5000.02) == (None, factory.paContinue)
        system.emit([[32767, -16384], [0, 0]], adc=8000.02)
        wait_for(lambda: len(blocks) == 2)
        assert [b.start for b in blocks] == pytest.approx([1.02, 1.02])
        assert blocks[0].samples.dtype == np.float32
        assert blocks[0].samples.shape == (2, 1) and blocks[1].samples.shape == (2, 2)
        assert blocks[0].samples[:, 0].tolist() == [-1.0, 0.5]
        assert blocks[1].samples[0, 1] == -0.5
        assert len(levels) == 2 and not errors
    finally:
        capture.stop()


def test_silence_and_no_loopback_callbacks_are_valid(fake):
    factory, _ = fake
    capture, blocks, errors, levels = group(fake)
    try:
        capture.start()
        factory.streams[0].emit([[0]] * 100)
        wait_for(lambda: len(blocks) == 1)
        time.sleep(0.35)
        assert capture.state == "running" and not errors
        assert levels == [("microphone", 0.0)]
    finally:
        capture.stop()


def test_pause_physically_closes_and_resume_keeps_selection_and_pause_gap(fake):
    factory, now = fake
    capture, blocks, errors, _ = group(fake)
    capture.start()
    previous = list(factory.streams)
    previous[0].emit([[100]], adc=5000.0)
    wait_for(lambda: len(blocks) == 1)
    capture.pause()
    assert all(s.closed and not s.active and s.stops for s in previous)
    assert factory.managers[-1].terminated and capture.state == "paused"
    assert previous[0].emit([[100]]) == (None, factory.paAbort)
    factory.default_input = 0  # A changed default must not broaden capture.
    now[0] = 105.0
    try:
        capture.resume()
        current = factory.streams[-2:]
        assert [s.kwargs["input_device_index"] for s in current] == [1, 3]
        assert previous[0].emit([[100]]) == (None, factory.paAbort)
        current[0].emit([[100]], adc=5000.0)
        wait_for(lambda: len(blocks) == 2)
        assert blocks[-1].start == pytest.approx(6.0) and blocks[-1].discontinuity
        assert not errors
    finally:
        capture.stop()
    assert all(s.closed for s in factory.streams)


def test_stop_drains_already_captured_tail(fake):
    factory, _ = fake
    capture, blocks, _, _ = group(fake, sources=("microphone",))
    capture.start()
    stream = factory.streams[-1]
    for index in range(5):
        stream.emit([[100]] * 100, adc=5000.0 + index * 100 / 44100)
    capture.stop()
    assert len(blocks) == 5


def test_selection_replacement_is_rejected_before_any_capture(fake):
    factory, _ = fake
    capture, _, _, _ = group(fake)
    factory.infos[3] = {**factory.infos[3], "name": "Replacement endpoint"}
    with pytest.raises(audio.AudioCaptureError, match="изменилось или отключено"):
        capture.start()
    assert not factory.streams and factory.managers[-1].terminated


def test_rate_tampering_and_missing_fingerprint_rejected(fake):
    factory, _ = fake
    device = audio.list_devices()["microphone"][0]
    with pytest.raises(audio.AudioCaptureError, match="Обновите список"):
        audio.CaptureGroup([replace(device, fingerprint="")], lambda block: None, lambda error: None, lambda source, level: None, 99)
    capture = audio.CaptureGroup([replace(device, sample_rate=16000)], lambda block: None, lambda error: None, lambda source, level: None, 99)
    with pytest.raises(audio.AudioCaptureError, match="Параметры"):
        capture.start()
    assert not factory.streams


def test_disconnect_is_fatal_once_and_never_opens_default(fake):
    factory, _ = fake
    capture, _, errors, _ = group(fake)
    capture.start()
    factory.streams[1].active = False
    wait_for(lambda: len(errors) == 1)
    assert capture.state == "fault" and all(s.closed for s in factory.streams)
    assert len(factory.streams) == 2
    assert "отключено" in errors[0]
    capture.stop()


def test_overflow_stops_both_channels_and_emits_safe_error(fake):
    factory, _ = fake
    capture, blocks, errors, _ = group(fake)
    capture.start()
    assert factory.streams[0].emit([[100]], status=2) == (None, factory.paAbort)
    wait_for(lambda: len(errors) == 1)
    assert not blocks and all(s.closed for s in factory.streams)
    assert "потерю звука" in errors[0] and "private" not in errors[0]
    capture.stop()


def test_callback_buffers_are_bounded_and_queue_overload_visible(fake):
    factory, _ = fake
    entered, release = threading.Event(), threading.Event()
    def slow_handler(block):
        entered.set()
        release.wait(2)
    capture, _, errors, _ = group(fake, sources=("microphone",), on_block=slow_handler)
    capture.start()
    stream = factory.streams[-1]
    stream.emit([[100]])
    assert entered.wait(1)
    try:
        for index in range(audio.MAX_QUEUED_BLOCKS):
            assert stream.emit([[100]], adc=5000.0 + (index + 1) / 44100)[1] == factory.paContinue
        assert stream.emit([[100]], adc=5000.0 + 40 / 44100)[1] == factory.paAbort
        assert capture._blocks.qsize() <= audio.MAX_QUEUED_BLOCKS
    finally:
        release.set()
    wait_for(lambda: len(errors) == 1)
    assert "не успевает" in errors[0]
    capture.stop()


def test_invalid_callback_clock_and_oversized_buffers_abort(fake):
    factory, _ = fake
    for bad_clock in (True, False):
        capture, _, errors, _ = group(fake, sources=("microphone",))
        capture.start()
        stream = factory.streams[-1]
        if bad_clock:
            result = stream.emit([[100]], adc=float("nan"))
        else:
            result = stream.emit([[100]] * (audio.MAX_CALLBACK_FRAMES + 1))
        assert result == (None, factory.paAbort)
        wait_for(lambda: len(errors) == 1)
        capture.stop()


def test_timestamp_calibration_does_not_drift_with_delivery_latency(fake):
    factory, now = fake
    capture, blocks, _, _ = group(fake, sources=("microphone",))
    try:
        capture.start()
        stream = factory.streams[-1]
        stream.emit([[100]] * 441, adc=5000.0)
        now[0] = 130.0  # Callback delivery time cannot become audio time.
        stream.emit([[100]] * 441, adc=5000.01)
        wait_for(lambda: len(blocks) == 2)
        assert [b.start for b in blocks] == pytest.approx([1.0, 1.01])
        assert not blocks[1].discontinuity
    finally:
        capture.stop()


def test_capture_to_streaming_keeps_all_pcm_through_small_adc_jitter(fake):
    """Actual capture adapter -> actual processor, with independent ADC clocks.

    v0.1.0 allowed 6ms overlap here but the next layer rejected it at 2ms.
    These callbacks contain different, ordered PCM; none may be lost/replayed.
    """
    factory, now = fake
    calls, statuses, accepted = [], [], []

    class PCMModel:
        def transcribe_words(self, pcm, rate):
            calls.append((rate, pcm.copy()))
            return []

    processor = StreamProcessor(PCMModel(), lambda segment: None,
                                lambda kind, data: statuses.append(kind))
    def feed(block):
        accepted.append(processor.feed(block))
    capture, blocks, errors, _ = group(fake, on_block=feed)
    processor.start()
    try:
        capture.start()
        jitters = [0, -.006, .008, -.010, .007, -.009, .005, -.002]
        for index, jitter in enumerate(jitters):
            now[0] = 100 + index * .04
            for stream in factory.streams:
                rate, channels = stream.kwargs['rate'], stream.kwargs['channels']
                samples = np.full((round(rate * .04), channels), 1000 + index, dtype='<i2')
                assert stream.emit(samples, adc=stream.get_time() + index * .04 + jitter)[1] == factory.paContinue
        capture.stop()
        processor.stop()
        assert len(accepted) == len(jitters) * 2 and all(accepted)
        assert not errors and 'capture_timestamp' not in statuses
        assert 'discontinuity' not in statuses
        for rate in (44100, 48000):
            actual = np.concatenate([pcm for recorded_rate, pcm in calls if recorded_rate == rate])
            expected = np.concatenate([np.full(round(rate * .04), (1000 + i) / 32768, dtype=np.float32)
                                       for i in range(len(jitters))])
            np.testing.assert_array_equal(actual, expected)
        assert not blocks and processor.pending_seconds == 0
    finally:
        capture.stop()
        processor.stop()


def test_real_loopback_silence_gap_is_not_collapsed(fake):
    factory, now = fake
    capture, blocks, errors, _ = group(fake, sources=("system",))
    try:
        capture.start()
        stream = factory.streams[-1]
        stream.emit([[100, 200]] * 1920, adc=8000.0)
        now[0] = 101.0
        stream.emit([[300, 400]] * 1920, adc=8001.0)
        capture.stop()
        assert [b.start for b in blocks] == pytest.approx([1.0, 2.0])
        assert blocks[-1].discontinuity and not errors
        assert [len(b.samples) for b in blocks] == [1920, 1920]
    finally:
        capture.stop()


def test_large_clock_reversal_still_aborts(fake):
    factory, _ = fake
    capture, _, errors, _ = group(fake, sources=("microphone",))
    try:
        capture.start()
        stream = factory.streams[-1]
        stream.emit([[100]] * 1764, adc=5000.0)
        assert stream.emit([[200]] * 1764, adc=4999.99)[1] == factory.paAbort
        wait_for(lambda: len(errors) == 1)
        assert 'часы' in errors[0]
    finally:
        capture.stop()


def test_jitter_near_origin_preserves_later_pcm_instead_of_trimming(fake):
    factory, _ = fake
    capture, blocks, errors, _ = group(fake, sources=("microphone",))
    try:
        capture.start()
        stream = factory.streams[-1]
        stream.emit([[100]] * 441, adc=4999.005)
        stream.emit([[200]] * 441, adc=4998.995)
        capture.stop()
        assert len(blocks) == 2 and not errors
        assert [len(b.samples) for b in blocks] == [441, 441]
        assert blocks[1].start == pytest.approx(blocks[0].start + .01)
    finally:
        capture.stop()


def test_missing_capture_dependency_gives_friendly_error(monkeypatch):
    def unavailable(name):
        raise ImportError("private environment details")
    monkeypatch.setattr(audio.importlib, "import_module", unavailable)
    with pytest.raises(audio.AudioCaptureError, match="WASAPI") as error:
        audio.list_devices()
    assert "private" not in str(error.value)


def test_unavailable_adc_clock_does_not_silently_discard_every_buffer(fake):
    factory, _ = fake
    capture, blocks, errors, _ = group(fake, sources=("microphone",))
    capture.start()
    assert factory.streams[-1].emit([[100]], adc=0)[1] == factory.paAbort
    wait_for(lambda: len(errors) == 1)
    assert not blocks
    capture.stop()


def test_handler_failure_closes_both_streams_without_driver_details(fake):
    factory, _ = fake
    def bad_handler(block):
        raise ValueError("private speech text must never be exposed")
    capture, _, errors, _ = group(fake, on_block=bad_handler)
    capture.start()
    factory.streams[0].emit([[100]])
    wait_for(lambda: len(errors) == 1)
    assert all(stream.closed for stream in factory.streams)
    assert "Обработчик" in errors[0] and "private" not in errors[0]
    capture.stop()


def test_unsupported_selected_format_fails_without_fallback(fake):
    factory, _ = fake
    capture, _, _, _ = group(fake)
    factory.unsupported = True
    with pytest.raises(audio.AudioCaptureError, match="не поддерживает"):
        capture.start()
    assert not factory.streams


def test_audio_before_session_origin_is_trimmed(fake):
    factory, _ = fake
    capture, blocks, errors, _ = group(fake, sources=("microphone",))
    try:
        capture.start()
        factory.streams[-1].emit([[100]] * 100, adc=4998.999)
        wait_for(lambda: len(blocks) == 1)
        assert 0 <= blocks[0].start < 1 / 44100
        assert 0 < len(blocks[0].samples) < 100
        assert not errors
    finally:
        capture.stop()


def test_cancel_during_second_source_start_aborts_first_callback_promptly(fake, monkeypatch):
    factory, _ = fake
    capture, blocks, fatal_errors, _ = group(fake)
    capture.cancel_event = threading.Event()
    first_started, second_stalled, release = threading.Event(), threading.Event(), threading.Event()
    startup_errors = []
    original_start = Stream.start_stream

    def stalled_start(stream):
        if stream.kwargs["input_device_index"] == 1:
            original_start(stream)
            assert stream.emit([[100]])[1] == factory.paContinue
            first_started.set()
        else:
            second_stalled.set()
            assert release.wait(3)
            original_start(stream)

    monkeypatch.setattr(Stream, "start_stream", stalled_start)
    def start():
        try:
            capture.start()
        except audio.AudioCaptureError as error:
            startup_errors.append(str(error))
    starter = threading.Thread(target=start)
    starter.start()
    try:
        assert first_started.wait(1) and second_stalled.wait(1)
        capture.cancel_event.set()
        before = time.monotonic()
        assert factory.streams[0].emit([[100]]) == (None, factory.paAbort)
        assert time.monotonic() - before < 0.1
        assert not factory.streams[0].active and not capture._recording.is_set()
        assert starter.is_alive(), 'first callback abort does not wait for the stalled second driver'
    finally:
        release.set()
        starter.join(2)
    assert not starter.is_alive()
    assert startup_errors == ["Запуск записи отменён."]
    assert not fatal_errors and not blocks
    assert all(stream.closed for stream in factory.streams)
    assert factory.managers[-1].terminated and capture._blocks.empty()
    assert capture.state == "stopped"


def test_runtime_cancel_is_not_reported_as_device_disconnect(fake):
    factory, _ = fake
    capture, _, fatal_errors, _ = group(fake)
    capture.cancel_event = threading.Event()
    capture.start()
    capture.cancel_event.set()
    assert factory.streams[0].emit([[100]]) == (None, factory.paAbort)
    wait_for(lambda: capture.state == "stopped")
    capture.stop()
    assert all(stream.closed for stream in factory.streams)
    assert not fatal_errors
