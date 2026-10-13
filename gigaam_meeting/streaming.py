"""Bounded, per-source PCM windows and one offline inference worker.

No diarization or acoustic echo cancellation is performed. Microphone and
loopback stay separate, including if they happen to hear the same loudspeaker.
Words belong to disjoint time ranges; text alone never triggers deduplication.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import threading
from time import monotonic
from typing import Callable

import numpy as np

from .contracts import AudioBlock, Segment, Source, Word

WINDOW_SECONDS = 8.0
OVERLAP_SECONDS = 1.0
PAUSE_SECONDS = 1.2
QUIET_RMS = 0.00001
TIMESTAMP_TOLERANCE = 0.002


@dataclass
class _SourceState:
    rate: int = 0
    parts: list[np.ndarray] = field(default_factory=list)
    frames: int = 0
    start: float = 0.0
    own_start: float = 0.0
    previous_end: float | None = None
    quiet_seconds: float = 0.0
    dropped: bool = False
    overloaded: bool = False
    last_received: float | None = None


@dataclass(frozen=True)
class _Window:
    source: Source
    pcm: np.ndarray
    rate: int
    start: float
    own_start: float
    own_end: float

    @property
    def duration(self) -> float:
        return len(self.pcm) / self.rate


@dataclass(frozen=True)
class _ContextWord:
    word: Word
    confirmed: bool


def _overlap_alignment(previous: list[_ContextWord], current: list[Word], overlap_start: float) -> dict[int, _ContextWord]:
    """Ordered, one-to-one matching with a shared bounded timing displacement.

    Independent CTC windows can move ALL overlap word times by ~250ms. A
    displacement is accepted only with at least two distinct exact tokens and
    consistent time/order evidence. Full previous context includes unconfirmed
    words, so a new real repetition matches its own slot instead of an older
    confirmed occurrence. Single-token/ambiguous overlap uses the stricter
    interval guard in _publish; this never collapses arbitrary repeated text.
    """
    drift_limit, residual_limit = .4, .12
    prior = [entry for entry in previous if entry.word.end >= overlap_start - drift_limit
             and entry.word.start <= overlap_start + OVERLAP_SECONDS + drift_limit]
    prefix = [(index, word) for index, word in enumerate(current)
              if word.start <= overlap_start + OVERLAP_SECONDS + drift_limit]
    if len({entry.word.text.casefold() for entry in prior} & {word.text.casefold() for _, word in prefix}) < 2:
        return {}
    def centers(word):
        return (word.start + word.end) / 2
    offsets = {round((centers(word) - centers(entry.word)) / .02) * .02
               for entry in prior for _, word in prefix
               if entry.word.text.casefold() == word.text.casefold()
               and abs(centers(word) - centers(entry.word)) <= drift_limit}
    best_score = 0.0
    best_pairs = []
    for shift in offsets:
        # A small overlap has few words; this bounded DP is independent of
        # session length and resolves repeated tokens without matching twice.
        rows = [[(0.0, []) for _ in range(len(prefix) + 1)] for _ in range(len(prior) + 1)]
        for i, entry in enumerate(prior, 1):
            for j, (index, word) in enumerate(prefix, 1):
                result = max(rows[i - 1][j], rows[i][j - 1], key=lambda cell: cell[0])
                residual = abs(centers(word) - shift - centers(entry.word))
                if entry.word.text.casefold() == word.text.casefold() and residual <= residual_limit:
                    intersection = min(word.end - shift, entry.word.end) - max(word.start - shift, entry.word.start)
                    longest = max(word.end - word.start, entry.word.end - entry.word.start)
                    if intersection > 0 and longest > 0:
                        score, pairs = rows[i - 1][j - 1]
                        candidate = (score + 2 + intersection / longest - residual * 3, pairs + [(index, entry)])
                        if candidate[0] > result[0]:
                            result = candidate
                rows[i][j] = result
        score, pairs = rows[-1][-1]
        score -= abs(shift) * .1  # Prefer the closest timing match on equal evidence.
        if len(pairs) >= 2 and len({entry.word.text.casefold() for _, entry in pairs}) >= 2 and score > best_score:
            best_score, best_pairs = score, pairs
    return dict(best_pairs)


class StreamProcessor:
    """Nonblocking capture input; serialized model calls and bounded PCM memory.

    ``feed`` returns False when input was rejected. Status callbacks carry safe
    messages and metadata, not audio, recognized words, or raw exception text.
    The duration budget includes active inference, queued windows and buffers.
    """

    def __init__(self, transcriber, emit_segment: Callable[[Segment], None],
                 emit_status: Callable[[str, dict], None], max_pending_seconds=48,
                 *, idle_timeout_seconds=1.2):
        if not math.isfinite(max_pending_seconds) or max_pending_seconds < 2 * (WINDOW_SECONDS + OVERLAP_SECONDS):
            raise ValueError("Очередь должна вмещать не менее 18 секунд звука.")
        if not math.isfinite(idle_timeout_seconds) or not .05 <= idle_timeout_seconds <= 30:
            raise ValueError("Таймаут ожидания звука должен быть от 0,05 до 30 секунд.")
        self.transcriber = transcriber
        self.emit_segment = emit_segment
        self.emit_status = emit_status
        self.max_pending_seconds = float(max_pending_seconds)
        self._idle_timeout = float(idle_timeout_seconds)
        self._condition = threading.Condition(threading.RLock())
        self._states = {source: _SourceState() for source in ("microphone", "system")}
        # Only overlap context, never a transcript archive. Accessed by the one
        # inference worker and cleared after it drains on stop.
        self._recent_words = {source: deque(maxlen=128) for source in self._states}
        self._queue: deque[_Window] = deque()
        self._work_seconds = 0.0
        self._thread: threading.Thread | None = None
        self._started = False
        self._closing = False
        self._closed = False

    @property
    def pending_seconds(self) -> float:
        with self._condition:
            return self._pending_locked()

    def _pending_locked(self) -> float:
        return self._work_seconds + sum(s.frames / s.rate for s in self._states.values() if s.rate)

    def _status(self, kind: str, source: Source | None, message: str, **data) -> None:
        try:
            self.emit_status(kind, {"source": source, "message": message, **data})
        except Exception:
            # UI/reporting callbacks must never kill capture or the worker.
            pass

    def start(self) -> None:
        with self._condition:
            if self._closing or self._closed:
                raise RuntimeError("Обработчик уже остановлен. Создайте новую сессию.")
            if self._started:
                return
            self._started = True
            self._thread = threading.Thread(target=self._run, name="gigaam-inference", daemon=True)
            self._thread.start()

    def feed(self, block: AudioBlock) -> bool:
        """Copy a small capture block; never wait for inference or queue space."""
        source = block.source
        if source not in self._states:
            self._status("invalid_audio", None, "Неизвестный источник звука.")
            return False
        if (not isinstance(block.sample_rate, int) or not 8000 <= block.sample_rate <= 192000
                or not math.isfinite(block.start) or block.start < 0):
            self._status("invalid_audio", source, "Некорректная частота или время звукового блока.")
            return False
        try:
            samples = np.asarray(block.samples)
            if (samples.ndim not in (1, 2) or (samples.ndim == 2 and not 1 <= samples.shape[1] <= 8)
                    or len(samples) / block.sample_rate > WINDOW_SECONDS):
                raise ValueError
            if not len(samples):
                return True
            # Downmix one device's channels; never mix the two source clocks.
            if not np.isfinite(samples).all() or np.max(np.abs(samples)) > 1.01:
                raise ValueError
            pcm = np.asarray(samples, dtype=np.float32)
            if pcm.ndim == 2:
                pcm = np.mean(pcm, axis=1, dtype=np.float32)
        except (TypeError, ValueError, OverflowError):
            self._status("invalid_audio", source, "Звуковой блок повреждён или слишком велик.")
            return False
        with self._condition:
            if not self._started or self._closing or self._closed:
                return False
            state = self._states[source]
            duration = len(pcm) / block.sample_rate
            end = block.start + duration
            if state.previous_end is not None and block.start < state.previous_end - TIMESTAMP_TOLERANCE:
                # Reject overlapping timestamps, rather than duplicating real PCM
                # or deleting legitimate repetitions based on recognized text.
                self._status("capture_timestamp", source, "Повторный или устаревший блок звука пропущен.")
                return False
            discontinuity = (block.discontinuity or state.dropped or (state.rate and state.rate != block.sample_rate)
                             or (state.previous_end is not None and block.start > state.previous_end + TIMESTAMP_TOLERANCE))
            if discontinuity:
                self._flush_locked(source, final=True)
            state.previous_end = end
            state.rate = block.sample_rate
            if not state.frames and not np.any(pcm):
                return True  # Leading digital silence consumes no inference budget.
            # Reserve retained overlap at every prospective hard window seam.
            window_frames = round(WINDOW_SECONDS * state.rate)
            step_frames = round((WINDOW_SECONDS - OVERLAP_SECONDS) * state.rate)
            prospective_frames = state.frames + len(pcm)
            future_seams = 0 if prospective_frames < window_frames else 1 + (prospective_frames - window_frames) // step_frames
            required = duration + future_seams * OVERLAP_SECONDS
            if self._pending_locked() + required > self.max_pending_seconds + 1e-7:
                if not state.overloaded:
                    self._status("overload", source, "Распознавание не успевает за звуком. Часть новых блоков пропущена.",
                                 pending_seconds=round(self._pending_locked(), 2))
                state.overloaded = True
                state.dropped = True
                return False
            if discontinuity:
                self._status("discontinuity", source, "В звуковом потоке обнаружен пропуск; фрагменты сохранены раздельно.")
                state.dropped = False
            state.overloaded = False
            state.last_received = monotonic()
            offset = 0
            while offset < len(pcm):
                size = min(round(WINDOW_SECONDS * state.rate) - state.frames, len(pcm) - offset)
                piece = pcm[offset:offset + size]
                rms = float(np.sqrt(np.mean(piece.astype(np.float64) ** 2)))
                if not state.frames and not np.any(piece):
                    offset += size
                    continue
                if not state.frames:
                    state.start = block.start + offset / state.rate
                    state.own_start = state.start
                state.parts.append(piece.copy())
                state.frames += size
                offset += size
                state.quiet_seconds = state.quiet_seconds + size / state.rate if rms < QUIET_RMS else 0.0
                if state.frames >= round(WINDOW_SECONDS * state.rate):
                    self._flush_locked(source, final=False)
                elif state.quiet_seconds >= PAUSE_SECONDS:
                    self._flush_locked(source, final=True)
            return True

    def _flush_locked(self, source: Source, *, final: bool) -> None:
        state = self._states[source]
        if not state.frames:
            return
        pcm = np.concatenate(state.parts)
        end = state.start + len(pcm) / state.rate
        own_end = end if final else end - OVERLAP_SECONDS / 2
        # After a hard seam the retained context is owned by the preceding
        # window. Do not infer or publish it again when paused immediately.
        if own_end > state.own_start + 1e-8:
            self._queue.append(_Window(source, pcm, state.rate, state.start, state.own_start, own_end))
            self._work_seconds += len(pcm) / state.rate
            self._condition.notify()
        if final:
            state.parts = []
            state.frames = 0
            state.quiet_seconds = 0.0
            state.last_received = None
        else:
            overlap_frames = round(OVERLAP_SECONDS * state.rate)
            retained = pcm[-overlap_frames:].copy()
            state.parts = [retained]
            state.frames = len(retained)
            state.start = end - len(retained) / state.rate
            state.own_start = own_end

    def pause_flush(self) -> None:
        with self._condition:
            if self._closed:
                return
            for source in self._states:
                self._flush_locked(source, final=True)

    def stop(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closing = True
            self.pause_flush()
            self._condition.notify_all()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        with self._condition:
            self._closed = True
            for recent in self._recent_words.values():
                recent.clear()

    def _run(self) -> None:
        while True:
            with self._condition:
                while not self._queue and not self._closing:
                    # WASAPI loopback can stop callbacks when playback goes
                    # quiet. Finish partial PCM after inactivity; absence of
                    # callbacks is not a device-disconnect classification.
                    now = monotonic()
                    for source, state in self._states.items():
                        if state.frames and state.last_received is not None and now - state.last_received >= self._idle_timeout:
                            self._flush_locked(source, final=True)
                    if self._queue:
                        break
                    self._condition.wait(timeout=min(.2, self._idle_timeout))
                if not self._queue:
                    return
                window = self._queue.popleft()
            try:
                words = self.transcriber.transcribe_words(window.pcm, window.rate)
                self._publish(window, words)
            except Exception as error:
                self._status("inference_failed", window.source, "Не удалось распознать фрагмент. Обработка следующих фрагментов продолжится.",
                             error_type=type(error).__name__)
            finally:
                with self._condition:
                    self._work_seconds = max(0.0, self._work_seconds - window.duration)
                    pending = self._pending_locked()
                self._status("progress", window.source, "", pending_seconds=pending)

    def _publish(self, window: _Window, words: list[Word]) -> None:
        selected: list[Word] = []
        recent = self._recent_words[window.source]
        # Model timestamps can shift slightly between overlap inputs. Compare
        # only against previously confirmed words in the same source/clock.
        # A manually flushed/idle/new phrase has no retained PCM overlap. Its
        # legitimate immediate repetition must never match the prior phrase.
        prior = list(recent) if window.own_start > window.start + 1e-6 else []
        raw: list[Word] = []
        previous_start = -1.0
        for word in words:
            if (not isinstance(word.text, str) or not math.isfinite(word.start) or not math.isfinite(word.end)
                    or word.start < previous_start or word.start < 0 or word.end < word.start
                    or word.end > window.duration + 0.1):
                raise ValueError("invalid_model_word")
            previous_start = word.start
            start, end = window.start + word.start, window.start + word.end
            if word.text.strip():
                raw.append(Word(start, end, word.text.strip()))
        aligned = _overlap_alignment(prior, raw, window.start)
        context: list[_ContextWord] = []
        for index, word in enumerate(raw):
            previous = aligned.get(index)
            if previous is None:
                candidates = []
                for entry in prior:
                    earlier = entry.word
                    intersection = min(word.end, earlier.end) - max(word.start, earlier.start)
                    longest = max(word.end - word.start, earlier.end - earlier.start)
                    if earlier.text.casefold() == word.text.casefold() and longest > 0 and intersection > longest * .65:
                        candidates.append((intersection / longest, entry))
                if candidates:
                    previous = max(candidates, key=lambda item: item[0])[1]
            already_confirmed = bool(previous and previous.confirmed)
            owns = window.own_start <= (word.start + word.end) / 2 < window.own_end
            if owns and not already_confirmed:
                selected.append(Word(max(word.start, window.own_start), min(word.end, window.own_end), word.text))
            context.append(_ContextWord(word, already_confirmed or owns))
        if selected:
            self.emit_segment(Segment(window.source, selected[0].start, selected[-1].end,
                                      " ".join(word.text for word in selected)))
        recent.clear()
        recent.extend(context)
