"""Explicit session lifecycle; UI never waits for model, drivers, or inference."""
from concurrent.futures import ThreadPoolExecutor
import threading
from time import perf_counter

from .audio import AudioCaptureError, CaptureGroup, list_devices
from .contracts import Event
from .engine import EngineError, Transcriber
from .echo import EchoDuplicateFilter
from .recording import AudioRecorder
from .streaming import StreamProcessor
from .storage import validate_local_path


class SessionManager:
    def __init__(self, event_queue, *, transcriber_factory=Transcriber, capture_factory=CaptureGroup, device_provider=list_devices):
        self.events = event_queue
        self.state = "idle"
        self.segments = []
        self._transcriber_factory = transcriber_factory
        self._capture_factory = capture_factory
        self._device_provider = device_provider
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-control")
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self._capture = self._processor = self._recorder = self._transcriber = None
        self._last_level = {}
        self._last_lag = 0
        self._stop_pending = False
        self._closed = False
        self._error = None
        self._echo = None

    def _event(self, kind, message="", **data):
        self.events.put(Event(kind, message, data))

    def _state(self, value):
        self.state = value
        self._event("state", state=value)

    def devices(self):
        # Enumeration opens no capture streams and accesses no audio content.
        return self._device_provider()

    def _reserve(self, config, state):
        with self._lock:
            if self._closed or self.state not in {"idle", "error"}:
                raise AudioCaptureError("Сначала завершите текущий сеанс.")
            if not config.consent or not (config.microphone or config.system):
                raise AudioCaptureError("Выберите источники и явно разрешите захват.")
            if config.microphone and config.microphone.source != "microphone" or config.system and config.system.source != "system":
                raise AudioCaptureError("Неверный тип источника. Обновите список устройств.")
            validate_local_path(config.output_folder)
            self._cancel.clear()
            self._stop_pending = False
            self._error = None
            self._state(state)

    def start(self, config):
        with self._lock:
            self._reserve(config, "loading")
            self.segments = []
            self._echo = EchoDuplicateFilter() if config.reduce_echo and config.microphone and config.system else None
            self._executor.submit(self._start, config)

    def _start(self, config):
        try:
            if self._transcriber is None:
                self._transcriber = self._transcriber_factory()
            self._transcriber.load()
            if self._cancel.is_set():
                return
            self._processor = StreamProcessor(self._transcriber, self._segment, self._status)
            self._processor.start()
            if config.save_audio:
                self._recorder = AudioRecorder(config.output_folder, self._fault)
            self._capture = self._capture_factory([d for d in (config.microphone, config.system) if d],
                self._block, self._fault, self._level, perf_counter())
            self._capture.cancel_event = self._cancel
            # Stop during slow model initialization never opens an audio stream.
            if self._cancel.is_set():
                return
            self._state("starting")
            self._capture.start()
            with self._lock:
                if self._cancel.is_set():
                    return
                self._state("recording")
        except (EngineError, AudioCaptureError) as exc:
            if not self._cancel.is_set():
                self._fault(str(exc))
        except Exception:
            self._fault("Не удалось запустить локальное распознавание. Проверьте целостность ZIP и доступную память.")

    def preview(self, config, seconds=3):
        with self._lock:
            self._reserve(config, "preview")
            self._executor.submit(self._preview, config, min(3, max(0.1, seconds)))

    def _preview(self, config, seconds):
        deadline = None
        try:
            self._capture = self._capture_factory([d for d in (config.microphone, config.system) if d],
                lambda block: None, self._fault, self._level, perf_counter())
            self._capture.cancel_event = self._cancel
            if self._cancel.is_set():
                return
            # A driver can stall while starting the second source. The deadline
            # cancels native callbacks independently of the control worker.
            deadline = threading.Timer(seconds, self.stop)
            deadline.daemon = True
            deadline.start()
            self._capture.start()
            self._cancel.wait(seconds)
        except AudioCaptureError as exc:
            if not self._cancel.is_set():
                self._fault(str(exc))
        except Exception:
            self._fault("Проверка устройств завершилась ошибкой. Обновите список и проверьте подключение.")
        finally:
            if deadline:
                deadline.cancel()
            self.stop()

    def _block(self, block):
        # Already captured owned blocks are accepted during pause/stop draining.
        echo = self._echo
        if echo:
            echo.feed(block)
        if self._processor:
            self._processor.feed(block)
        if self._recorder:
            self._recorder.feed(block)
        now = perf_counter()
        if self._processor and now - self._last_lag > 1:
            self._last_lag = now
            self._event("lag", pending_seconds=self._processor.pending_seconds)

    def _level(self, source, value):
        now = perf_counter()
        if now - self._last_level.get(source, 0) >= 0.1 and self.events.qsize() < 128:
            self._last_level[source] = now
            self._event("level", source=source, level=value)

    def _segment(self, segment):
        removed, added = self._echo.accept(segment) if self._echo else ([], [segment])
        with self._lock:
            for old in removed:
                if old in self.segments:
                    self.segments.remove(old)
            self.segments.extend(added)
            self.segments.sort(key=lambda s: (s.start, s.end, s.source))
        if removed:
            self._event("echo_update", removed=removed, added=added)
        else:
            for item in added:
                self._event("segment", segment=item)
        if self._echo:
            self._event("echo_stats", hidden_words=self._echo.hidden_words)

    def _status(self, kind, data):
        if kind == "progress":
            self._event("lag", pending_seconds=data.get("pending_seconds", 0))
        elif kind in {"inference_failed", "invalid_audio", "capture_timestamp"}:
            self._fault(data.get("message", "Ошибка обработки звука."))
        else:
            self._event("warning", data.get("message", "Пропуск аудио."))

    def _fault(self, message):
        with self._lock:
            if self._error is None:
                self._error = message
        self.stop()

    def pause(self):
        self._executor.submit(self._pause)

    def _pause(self):
        if self.state != "recording" or self._cancel.is_set():
            return
        try:
            self._capture.pause()
            self._processor.pause_flush()
            self._state("paused")
        except Exception:
            self._fault("Не удалось безопасно поставить захват на паузу. Сеанс остановлен.")

    def resume(self):
        self._executor.submit(self._resume)

    def _resume(self):
        if self.state != "paused" or self._cancel.is_set():
            return
        try:
            self._state("starting")
            self._capture.resume()
            with self._lock:
                if not self._cancel.is_set():
                    self._state("recording")
        except AudioCaptureError as exc:
            if not self._cancel.is_set():
                self._fault(str(exc))
        except Exception:
            self._fault("Устройство изменилось. Сеанс остановлен; выберите источники заново.")

    def stop(self):
        self._cancel.set()
        with self._lock:
            if self._stop_pending or self.state in {"idle", "error"}:
                return
            self._stop_pending = True
            self._executor.submit(self._finish)

    def _finish(self):
        self._state("stopping")
        try:
            # Each resource must be stopped even if an earlier driver fails.
            for index, resource in enumerate((self._capture, self._processor, self._recorder)):
                if index == 1:
                    self._state("draining")
                if resource:
                    try:
                        resource.stop()
                    except Exception:
                        self._error = self._error or "Ошибка завершения сеанса. Полученный текст доступен для экспорта."
        finally:
            with self._lock:
                if self._echo:
                    self._echo.clear()
                    self._echo = None
                self._capture = self._processor = self._recorder = None
                self._event("lag", pending_seconds=0)
                self._state("error" if self._error else "idle")
                if self._error:
                    self._event("error", self._error)
                self._event("finished")

    def close(self):
        with self._lock:
            self._closed = True
            self.stop()
            self._executor.shutdown(wait=False)
