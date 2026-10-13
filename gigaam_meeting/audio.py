"""Explicit WASAPI device capture; no meeting, process, or default-device fallback."""
from __future__ import annotations

import hashlib
import importlib
import json
import math
import queue
import threading
from dataclasses import dataclass
from time import monotonic, perf_counter
from typing import Callable

import numpy as np

from .contracts import AudioBlock, AudioDevice, SOURCE_LABELS

CAPTURE_SCOPE = "device"
SUPPORTS_PROCESS_CAPTURE = False
CAPTURE_NOTE = "Захватывается звук выбранного устройства целиком, включая другие приложения. Захват отдельного процесса не поддерживается."
DEVICE_CHANGE_NOTE = "При отключении устройства запись останавливается. После смены устройств обновите список и подтвердите выбор: автоматического переключения нет."
MAX_CALLBACK_FRAMES = 8192
MAX_QUEUED_BLOCKS = 32
ADC_TIMESTAMP_JITTER_SECONDS = 0.02


class AudioCaptureError(RuntimeError):
    """A safe, user-facing capture error, without driver dumps or audio content."""


def _backend():
    try:
        return importlib.import_module("pyaudiowpatch")
    except (ImportError, OSError):
        raise AudioCaptureError("Не удалось загрузить компоненты WASAPI. Полностью распакуйте проверенный Windows x64 ZIP в новую локальную папку и откройте EXE снова.") from None


def _fingerprint(info: dict) -> str:
    # PortAudio exposes no stable endpoint GUID. Match every available selection
    # characteristic and the exact index; never silently select a new default.
    fields = {key: info.get(key) for key in ("index", "name", "hostApi", "isLoopbackDevice", "maxInputChannels", "maxOutputChannels", "defaultSampleRate")}
    return hashlib.sha256(json.dumps(fields, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _device(info: dict, default_id: int) -> AudioDevice:
    return AudioDevice(id=int(info["index"]), name=str(info["name"]),
        source="system" if info.get("isLoopbackDevice") else "microphone",
        sample_rate=int(round(float(info["defaultSampleRate"]))),
        channels=min(2, int(info["maxInputChannels"])),
        is_default=int(info["index"]) == default_id, fingerprint=_fingerprint(info))


def list_devices() -> dict[str, list[AudioDevice]]:
    module = _backend()
    manager = None
    try:
        manager = module.PyAudio()
        host = manager.get_host_api_info_by_type(module.paWASAPI)
        default_loopback = -1
        try:
            default_loopback = int(manager.get_default_wasapi_loopback()["index"])
        except (OSError, ValueError, LookupError, KeyError):
            pass  # Defaults are display hints only, never capture fallbacks.
        result = {"microphone": [], "system": []}
        for index in range(manager.get_device_count()):
            info = manager.get_device_info_by_index(index)
            if info.get("hostApi") != host["index"] or int(info.get("maxInputChannels", 0)) < 1:
                continue
            default = default_loopback if info.get("isLoopbackDevice") else int(host.get("defaultInputDevice", -1))
            device = _device(info, default)
            if 8000 <= device.sample_rate <= 192000:
                result[device.source].append(device)
        return result
    except Exception:
        raise AudioCaptureError("Windows не предоставил список устройств WASAPI. Проверьте подключение устройств и доступ к микрофону.") from None
    finally:
        if manager is not None:
            try:
                manager.terminate()
            except Exception:
                pass


@dataclass
class _Clock:
    offset: float
    previous_end: float | None = None
    discontinuity: bool = False


class CaptureGroup:
    def __init__(self, devices: list[AudioDevice], on_block: Callable[[AudioBlock], None],
                 on_error: Callable[[str], None], on_level: Callable[[str, float], None], clock_origin: float):
        if not devices or len(devices) > 2 or len({d.source for d in devices}) != len(devices):
            raise AudioCaptureError("Выберите не более одного микрофона и одного устройства звука компьютера.")
        if not math.isfinite(clock_origin):
            raise AudioCaptureError("Не удалось установить часы записи.")
        if any(d.source not in SOURCE_LABELS or d.host_api != "WASAPI" or not d.fingerprint for d in devices):
            raise AudioCaptureError("Обновите список устройств и выберите устройства WASAPI заново.")
        self.devices = tuple(devices)
        self.on_block, self.on_error, self.on_level = on_block, on_error, on_level
        self.clock_origin = clock_origin
        self.cancel_event: threading.Event | None = None
        self._lock = threading.RLock()
        self._fault_lock = threading.Lock()
        self._recording = threading.Event()
        self._shutdown = threading.Event()
        self._blocks: queue.Queue[AudioBlock] = queue.Queue(MAX_QUEUED_BLOCKS)
        self._manager = None
        self._streams: list[tuple[AudioDevice, object]] = []
        self._worker: threading.Thread | None = None
        self._pending_error: str | None = None
        self._generation = 0
        self.state = "stopped"

    def _cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _check_cancel(self) -> None:
        if self._cancelled():
            self._recording.clear()
            raise AudioCaptureError("Запуск записи отменён.")

    def _fault(self, message: str) -> None:
        with self._fault_lock:
            if self._pending_error is None:
                self._pending_error = message
        self._recording.clear()

    def _callback(self, module, device: AudioDevice, clock: _Clock, generation: int):
        def receive(data, frame_count, time_info, status):
            if self._cancelled():
                self._recording.clear()
                return None, module.paAbort
            if not self._recording.is_set() or generation != self._generation:
                return None, module.paAbort
            label = SOURCE_LABELS[device.source]
            try:
                if status:
                    self._fault(f"{label}: драйвер сообщил потерю звука. Запись остановлена; проверьте устройство и возобновите её явно.")
                    return None, module.paAbort
                if not 0 < frame_count <= MAX_CALLBACK_FRAMES or not isinstance(data, bytes) or len(data) != frame_count * device.channels * 2:
                    self._fault(f"{label}: некорректный аудиобуфер. Запись остановлена.")
                    return None, module.paAbort
                adc = float(time_info["input_buffer_adc_time"])
                if not math.isfinite(adc):
                    raise ValueError("invalid clock")
                start = adc + clock.offset - self.clock_origin
                if start < -1 or start > perf_counter() - self.clock_origin + 1:
                    raise ValueError("unusable ADC clock")
                discontinuity = clock.discontinuity
                if clock.previous_end is not None:
                    delta = start - clock.previous_end
                    # WASAPI's estimated ADC timestamps can jitter even when
                    # callback PCM is ordered and distinct. v0.1.0 accepted
                    # 20ms here but passed the overlap to a 2ms downstream
                    # guard, causing a false fatal error. Keep every sample
                    # and advance by its exact frame count inside this bounded
                    # range. Genuine larger gaps stay explicit; larger clock
                    # reversals/driver overflow still stop capture.
                    jitter_limit = ADC_TIMESTAMP_JITTER_SECONDS + 2 / device.sample_rate
                    if delta < -jitter_limit:
                        self._fault(f"{label}: часы аудиоустройства сбились. Запись остановлена.")
                        return None, module.paAbort
                    if abs(delta) <= jitter_limit:
                        start = clock.previous_end
                    else:
                        discontinuity = True
                samples = np.frombuffer(data, dtype="<i2").reshape(frame_count, device.channels).astype(np.float32)
                samples *= np.float32(1 / 32768)
                if start < 0:
                    trim = min(frame_count, math.ceil(-start * device.sample_rate))
                    samples = samples[trim:].copy()
                    start += trim / device.sample_rate
                if not len(samples):
                    return None, module.paContinue
                clock.previous_end = start + len(samples) / device.sample_rate
                clock.discontinuity = False
                if self._cancelled():
                    self._recording.clear()
                    return None, module.paAbort
                self._blocks.put_nowait(AudioBlock(device.source, samples, device.sample_rate, start, discontinuity))
                return None, module.paContinue
            except queue.Full:
                self._fault("Обработка звука не успевает за записью. Запись остановлена, чтобы не скрывать пропуски; возобновите её после проверки нагрузки.")
            except (ValueError, TypeError, KeyError, OverflowError):
                self._fault(f"{label}: не удалось получить корректный звук или время устройства. Запись остановлена.")
            except Exception:
                # Never let PortAudio print a callback traceback with user data.
                self._fault(f"{label}: ошибка захвата звука. Запись остановлена.")
            return None, module.paAbort
        return receive

    def start(self) -> None:
        with self._lock:
            if self._cancelled():
                self._halt("stopped")
                raise AudioCaptureError("Запуск записи отменён.")
            if self.state == "running":
                return
            if self._worker is not None and self._worker.is_alive():
                raise AudioCaptureError("Предыдущая обработка звука ещё не завершилась. Дождитесь её завершения перед новой записью.")
            module = _backend()
            self._pending_error = None
            self._shutdown.clear()
            self._generation += 1
            try:
                self._check_cancel()
                self._manager = module.PyAudio()
                self._check_cancel()
                host = self._manager.get_host_api_info_by_type(module.paWASAPI)
                # Validate every choice before opening even the first channel.
                for device in self.devices:
                    self._check_cancel()
                    info = self._manager.get_device_info_by_index(device.id)
                    if info.get("hostApi") != host["index"] or _fingerprint(info) != device.fingerprint or bool(info.get("isLoopbackDevice")) != (device.source == "system"):
                        raise AudioCaptureError("Выбранное аудиоустройство изменилось или отключено. Обновите список и подтвердите выбор; автоматической замены нет.")
                    actual = _device(info, -1)
                    if (device.sample_rate, device.channels) != (actual.sample_rate, actual.channels):
                        raise AudioCaptureError("Параметры выбранного устройства изменились. Выберите его заново.")
                    if not self._manager.is_format_supported(device.sample_rate, input_device=device.id,
                        input_channels=device.channels, input_format=module.paInt16):
                        raise AudioCaptureError("Выбранное устройство не поддерживает требуемый формат звука.")
                for device in self.devices:
                    self._check_cancel()
                    clock = _Clock(0, discontinuity=self._generation > 1)
                    stream = self._manager.open(format=module.paInt16, channels=device.channels, rate=device.sample_rate,
                        input=True, input_device_index=device.id, frames_per_buffer=min(4096, max(256, device.sample_rate // 25)),
                        stream_callback=self._callback(module, device, clock, self._generation), start=False)
                    self._streams.append((device, stream))
                    self._check_cancel()
                    before = perf_counter()
                    portaudio_time = float(stream.get_time())
                    after = perf_counter()
                    if not math.isfinite(portaudio_time):
                        raise AudioCaptureError("Не удалось синхронизировать часы аудиоустройства.")
                    clock.offset = (before + after) / 2 - portaudio_time
                self._check_cancel()
                self._recording.set()
                for _, stream in self._streams:
                    self._check_cancel()
                    stream.start_stream()
                    self._check_cancel()
                self.state = "running"
                self._worker = threading.Thread(target=self._deliver, name="wasapi-delivery", daemon=True)
                self._worker.start()
            except Exception as exc:
                self._halt("stopped")
                if isinstance(exc, AudioCaptureError):
                    raise
                raise AudioCaptureError("Не удалось открыть выбранные устройства WASAPI. Проверьте доступ к микрофону, подключение и занятость устройства другим приложением.") from None

    def _deliver(self) -> None:
        next_check = 0.0
        while not self._shutdown.is_set() or not self._blocks.empty():
            cancelled = self._cancelled()
            if cancelled and not self._shutdown.is_set():
                self._halt("stopped", from_worker=True)
            error = self._pending_error
            if error is not None and not cancelled:
                self._halt("fault", from_worker=True)
                try:
                    self.on_error(error)
                except Exception:
                    pass
                return
            if not self._shutdown.is_set() and monotonic() >= next_check:
                next_check = monotonic() + 0.25
                # Silence is valid, including a loopback with no callbacks.
                # Only an actual stopped/erroring stream causes a fault.
                try:
                    with self._lock:
                        failed = self.state == "running" and any(not stream.is_active() for _, stream in self._streams)
                    if failed and not self._cancelled():
                        self._fault("Аудиоустройство отключено или остановило поток. Запись остановлена; выберите устройство или возобновите запись явно.")
                except Exception:
                    if not self._cancelled():
                        self._fault("Не удалось проверить аудиоустройство. Запись остановлена; проверьте подключение.")
                if self._pending_error is not None and not self._cancelled():
                    continue
            try:
                block = self._blocks.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.on_level(block.source, float(np.sqrt(np.mean(block.samples * block.samples))))
                self.on_block(block)
            except Exception:
                self._fault("Обработчик звука завершился с ошибкой. Запись остановлена.")

    def _halt(self, state: str, *, from_worker: bool = False) -> None:
        with self._lock:
            self._recording.clear()
            self.state = state
            streams, self._streams = self._streams, []
            for _, stream in streams:
                try:
                    stream.stop_stream()
                except Exception:
                    pass
                try:
                    stream.close()
                except Exception:
                    pass
            if self._manager is not None:
                try:
                    self._manager.terminate()
                except Exception:
                    pass
                self._manager = None
            self._shutdown.set()
            # Normal pause/stop drains already-owned audio before returning.
            # Faults explicitly reject the uncertain tail; they never hide loss.
            if state == "fault" or self._worker is None or not self._worker.is_alive():
                while True:
                    try:
                        self._blocks.get_nowait()
                    except queue.Empty:
                        break
            worker = self._worker
        if worker is not None and not from_worker and worker is not threading.current_thread():
            worker.join(timeout=2)

    def pause(self) -> None:
        self._halt("paused")

    def resume(self) -> None:
        self.start()

    def stop(self) -> None:
        self._halt("stopped")
