"""Conservative transcript-only echo reduction with bounded local PCM evidence.

This is not AEC: raw capture/optional WAVs are untouched. A microphone word is
removed only within an ordered, time-matched phrase of at least three words,
with a strong and unambiguous acoustic match to the output-device reference.
Uncertain echo, single words, missing reference and independent speech remain.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math
import threading

import numpy as np

from .contracts import AudioBlock, Segment, Word

RATE = 8000
HISTORY_SECONDS = 48
MAX_RECORDS = 64
MAX_WORDS = 128
MIN_DELAY, MAX_DELAY = -.15, .6
MIN_CORRELATION = .9


class EchoAudioHistory:
    def __init__(self):
        self.capacity = RATE * HISTORY_SECONDS
        self._values = {s: np.zeros(self.capacity, np.float32) for s in ("microphone", "system")}
        self._stamps = {s: np.full(self.capacity, -1, np.int64) for s in self._values}
        self._lock = threading.Lock()

    @property
    def memory_bytes(self):
        return sum(a.nbytes for a in (*self._values.values(), *self._stamps.values()))

    def feed(self, block: AudioBlock):
        pcm = np.asarray(block.samples, dtype=np.float32)
        if pcm.ndim == 2:
            pcm = pcm.mean(axis=1, dtype=np.float32)
        if (block.source not in self._values or pcm.ndim != 1 or not len(pcm)
                or len(pcm) > 8192 or not 8000 <= block.sample_rate <= 192000
                or not math.isfinite(block.start) or block.start < 0 or not np.isfinite(pcm).all()):
            return
        # Mild anti-alias smoothing; evidence never replaces model input PCM.
        width = max(1, round(block.sample_rate / RATE))
        if width > 1:
            pcm = np.convolve(np.pad(pcm, (width // 2, width - 1 - width // 2), mode="edge"),
                              np.full(width, 1 / width, np.float32), mode="valid")
        first = math.ceil(block.start * RATE - .5)
        last = math.ceil((block.start + len(pcm) / block.sample_rate) * RATE - .5)
        stamps = np.arange(first, last, dtype=np.int64)
        positions = ((stamps + .5) / RATE - block.start) * block.sample_rate
        values = np.interp(positions, np.arange(len(pcm)), pcm).astype(np.float32)
        indices = stamps % self.capacity
        with self._lock:
            self._values[block.source][indices] = values
            self._stamps[block.source][indices] = stamps

    def _read_locked(self, source, first, last):
        stamps = np.arange(first, last, dtype=np.int64)
        indices = stamps % self.capacity
        valid = (stamps >= 0) & (self._stamps[source][indices] == stamps)
        values = self._values[source][indices].astype(np.float64)
        values[~valid] = 0
        return values, valid

    def matches(self, start: float, end: float) -> bool:
        # Limit work and use at least 240ms of evidence around a short word.
        if not math.isfinite(start + end) or end < start:
            return False
        center = (start + end) / 2
        duration = min(1.2, max(.24, end - start))
        first = round((center - duration / 2) * RATE)
        last = first + round(duration * RATE)
        ref_first = first - round(MAX_DELAY * RATE)
        ref_last = last - round(MIN_DELAY * RATE)
        with self._lock:
            x, valid_x = self._read_locked("microphone", first, last)
            y, valid_y = self._read_locked("system", ref_first, ref_last)
        if not valid_x.all():
            return False
        x -= x.mean()
        x_energy = float(np.dot(x, x))
        n = len(x)
        if x_energy / n < 1e-8:
            return False
        # FFT normalized cross-correlation over a bounded possible echo delay.
        size = 1 << (len(x) + len(y) - 2).bit_length()
        dots = np.fft.irfft(np.fft.rfft(y, size) * np.fft.rfft(x[::-1], size), size)[n - 1:len(y)]
        sums = np.r_[0., np.cumsum(y)]
        powers = np.r_[0., np.cumsum(y * y)]
        counts = np.r_[0, np.cumsum(valid_y)]
        energy = powers[n:] - powers[:-n] - (sums[n:] - sums[:-n]) ** 2 / n
        eligible = ((counts[n:] - counts[:-n]) == n) & (energy / n >= 1e-8)
        scores = np.zeros(len(dots))
        scores[eligible] = np.abs(dots[eligible]) / np.sqrt(x_energy * energy[eligible])
        peak = int(np.argmax(scores))
        if scores[peak] < MIN_CORRELATION:
            return False
        # Periodic tones or multiple near-identical reference locations provide
        # ambiguous evidence, even if their maximum correlation is high.
        outside = np.abs(np.arange(len(scores)) - peak) > round(.08 * RATE)
        return not outside.any() or scores[outside].max(initial=0) < scores[peak] - .02

    def clear(self):
        with self._lock:
            for values in self._values.values():
                values.fill(0)
            for stamps in self._stamps.values():
                stamps.fill(-1)


def _token(text):
    return "".join(c for c in text.casefold() if c.isalnum())


def _pairs(microphone: tuple[Word, ...], reference: list[Word]):
    """Ordered one-to-one matching, without fuzzy words or speaker inference."""
    rows = [[[] for _ in range(len(reference) + 1)] for _ in range(len(microphone) + 1)]
    for i, mic in enumerate(microphone, 1):
        for j, system in enumerate(reference, 1):
            best = max(rows[i - 1][j], rows[i][j - 1], key=len)
            if (_token(mic.text) and _token(mic.text) == _token(system.text)
                    and abs((mic.start + mic.end - system.start - system.end) / 2) <= .65):
                matched = rows[i - 1][j - 1] + [(i - 1, j - 1)]
                if len(matched) > len(best):
                    best = matched
            rows[i][j] = best
    result = rows[-1][-1]
    if len(result) < 3 or len({_token(microphone[i].text) for i, _ in result}) < 2:
        return []
    return result


def _plain(segment):
    return Segment(segment.source, segment.start, segment.end, segment.text)


@dataclass
class _MicRecord:
    original: Segment
    visible: list[Segment]
    hidden: set[int] = field(default_factory=set)


class EchoDuplicateFilter:
    def __init__(self):
        self.audio = EchoAudioHistory()
        self._microphone: deque[_MicRecord] = deque(maxlen=MAX_RECORDS)
        self._system: deque[Segment] = deque(maxlen=MAX_RECORDS)
        self._latest = 0.
        self.hidden_words = 0

    def feed(self, block):
        self.audio.feed(block)

    def _reduce(self, record):
        original = record.original
        reference = sorted((word for s in self._system for word in s.words
                            if word.end >= original.start - .65 and word.start <= original.end + .65),
                           key=lambda w: (w.start, w.end))[:MAX_WORDS * 2]
        candidates = _pairs(original.words, reference)
        newly_hidden = {i for i, _ in candidates if i not in record.hidden
                        and self.audio.matches(original.words[i].start, original.words[i].end)}
        if not newly_hidden:
            return [], []
        record.hidden.update(newly_hidden)
        self.hidden_words += len(newly_hidden)
        groups, current = [], []
        for i, word in enumerate(original.words):
            if i in record.hidden:
                if current:
                    groups.append(current)
                    current = []
            else:
                current.append(word)
        if current:
            groups.append(current)
        visible = [Segment("microphone", words[0].start, words[-1].end, " ".join(w.text for w in words)) for words in groups]
        removed, record.visible = record.visible, visible
        return removed, visible

    def accept(self, segment: Segment):
        """Return bounded transcript edits. Either channel can arrive first."""
        self._latest = max(self._latest, segment.end)
        cutoff = self._latest - HISTORY_SECONDS
        self._microphone = deque((r for r in self._microphone if r.original.end >= cutoff), maxlen=MAX_RECORDS)
        self._system = deque((s for s in self._system if s.end >= cutoff), maxlen=MAX_RECORDS)
        if not segment.words or len(segment.words) > MAX_WORDS:
            return [], [_plain(segment)]
        removed, added = [], []
        if segment.source == "system":
            self._system.append(segment)
            added.append(_plain(segment))
            for record in self._microphone:
                if record.original.end >= segment.start - .65 and record.original.start <= segment.end + .65:
                    old, new = self._reduce(record)
                    removed.extend(old)
                    added.extend(new)
        else:
            record = _MicRecord(segment, [_plain(segment)])
            self._microphone.append(record)
            self._reduce(record)
            added.extend(record.visible)
        return removed, added

    def clear(self):
        self.audio.clear()
        self._microphone.clear()
        self._system.clear()
