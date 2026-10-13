"""Synthetic/public PCM only: no user's microphone, meeting or transcript."""
from pathlib import Path
import wave

import numpy as np
import pytest

from gigaam_meeting.contracts import AudioBlock, Segment, Word
from gigaam_meeting.echo import EchoAudioHistory, EchoDuplicateFilter, HISTORY_SECONDS, MAX_RECORDS, RATE


def signal(seed=1, seconds=7):
    rng = np.random.default_rng(seed)
    values = rng.normal(0, .15, round(seconds * RATE))
    values = np.convolve(values, np.full(7, 1 / 7), mode="same")
    time = np.arange(len(values)) / RATE
    return (values * (.3 + .7 * np.sin(11 * time) ** 2)).astype(np.float32)


def feed(history, reference, *, own=None, delay=.12, gain=.3, start=0, rates=(44100, 48000)):
    for source, rate in zip(("microphone", "system"), rates):
        count = round((len(reference) / RATE + max(delay, 0)) * rate)
        time = np.arange(count) / rate
        values = np.interp(time - (delay if source == "microphone" else 0),
                           np.arange(len(reference)) / RATE, reference, left=0, right=0)
        if source == "microphone":
            values *= gain
            if own is not None:
                values += np.interp(time, np.arange(len(own)) / RATE, own, left=0, right=0)
        size = round(.04 * rate)
        for i in range(0, count, size):
            pcm = values[i:i + size].astype(np.float32)
            # Stereo reference and mono microphone have different rates.
            if source == "system":
                pcm = np.column_stack([pcm, pcm])
            history.feed(AudioBlock(source, pcm, rate, start + i / rate))


def phrase(source, text="сегодня проверяем нашу речь", *, shift=0, extra=False):
    words = [Word(1 + i * .55 + shift, 1.32 + i * .55 + shift, token)
             for i, token in enumerate(text.split())]
    if extra:
        words += [Word(3.6, 3.9, "мой"), Word(4., 4.35, "ответ")]
    return Segment(source, words[0].start, words[-1].end, " ".join(w.text for w in words), tuple(words))


def apply(transcript, edits):
    removed, added = edits
    for item in removed:
        transcript.remove(item)
    transcript.extend(added)
    transcript.sort(key=lambda s: (s.start, s.end, s.source))


@pytest.mark.parametrize("system_first", [True, False])
@pytest.mark.parametrize("text", ["сегодня проверяем нашу речь", "бүгін қазақ тілін тексереміз", "today we test speech", "бүгін проверяем mixed speech"])
def test_echo_removed_in_both_arrival_orders_and_languages(system_first, text):
    reducer = EchoDuplicateFilter()
    feed(reducer.audio, signal())
    mic, system = phrase("microphone", text, shift=.12), phrase("system", text)
    transcript = []
    for segment in ([system, mic] if system_first else [mic, system]):
        apply(transcript, reducer.accept(segment))
    assert [(s.source, s.text) for s in transcript] == [("system", text)]
    assert reducer.hidden_words == len(text.split())


def test_identical_words_from_independent_simultaneous_voice_stay():
    reducer = EchoDuplicateFilter()
    feed(reducer.audio, signal(), own=signal(24), gain=0)
    transcript = []
    for segment in (phrase("system"), phrase("microphone")):
        apply(transcript, reducer.accept(segment))
    assert len(transcript) == 2 and reducer.hidden_words == 0


def test_double_talk_matching_words_stay_when_microphone_has_independent_voice():
    reducer = EchoDuplicateFilter()
    feed(reducer.audio, signal(), own=signal(24), gain=.3)
    transcript = []
    for segment in (phrase("system"), phrase("microphone", shift=.12)):
        apply(transcript, reducer.accept(segment))
    assert len(transcript) == 2 and reducer.hidden_words == 0


def test_unmatched_local_reply_is_preserved_with_original_word_times():
    reducer = EchoDuplicateFilter()
    own = signal(24)
    own[:round(3.5 * RATE)] = 0
    feed(reducer.audio, signal(), own=own)
    transcript = []
    mic = phrase("microphone", shift=.12, extra=True)
    for segment in (mic, phrase("system")):
        apply(transcript, reducer.accept(segment))
    local = [s for s in transcript if s.source == "microphone"]
    assert [(s.start, s.end, s.text) for s in local] == [(3.6, 4.35, "мой ответ")]
    assert reducer.hidden_words == 4


@pytest.mark.parametrize("text", ["да", "да да да", "one two"])
def test_short_or_ambiguous_words_are_never_deduplicated(text):
    reducer = EchoDuplicateFilter()
    feed(reducer.audio, signal())
    transcript = []
    for segment in (phrase("system", text), phrase("microphone", text, shift=.12)):
        apply(transcript, reducer.accept(segment))
    assert len(transcript) == 2 and reducer.hidden_words == 0


def test_repetition_later_in_time_and_missing_audio_evidence_stay():
    reducer = EchoDuplicateFilter()
    transcript = []
    for segment in (phrase("system"), phrase("microphone", shift=.12), phrase("microphone", shift=10)):
        apply(transcript, reducer.accept(segment))
    assert len(transcript) == 3 and reducer.hidden_words == 0


def test_silence_and_periodic_tones_are_not_echo_evidence():
    for values in (np.zeros(7 * RATE, np.float32),
                   np.sin(2 * np.pi * 400 * np.arange(7 * RATE) / RATE).astype(np.float32) * .1):
        history = EchoAudioHistory()
        feed(history, values)
        assert not history.matches(1.12, 1.44)


def test_missing_reference_window_and_ring_wrap_fail_open_and_clear():
    history = EchoAudioHistory()
    initial_bytes = history.memory_bytes
    feed(history, signal())
    assert history.matches(1.12, 1.44)
    # A late block reuses the same ring slots. Old data cannot masquerade as
    # current data across a pause, device switch or long session.
    feed(history, signal(), start=HISTORY_SECONDS)
    assert not history.matches(1.12, 1.44)
    assert history.matches(HISTORY_SECONDS + 1.12, HISTORY_SECONDS + 1.44)
    assert history.memory_bytes == initial_bytes == 2 * RATE * HISTORY_SECONDS * 12
    history.clear()
    assert not history.matches(HISTORY_SECONDS + 1.12, HISTORY_SECONDS + 1.44)
    assert all(not a.any() for a in history._values.values())


def test_reference_records_are_bounded_over_long_session():
    reducer = EchoDuplicateFilter()
    for i in range(200):
        reducer.accept(phrase("system", shift=i))
        reducer.accept(phrase("microphone", shift=i))
    assert len(reducer._system) <= MAX_RECORDS and len(reducer._microphone) <= MAX_RECORDS
    reducer.clear()
    assert not reducer._system and not reducer._microphone


@pytest.mark.parametrize("delay,gain", [(.08, .15), (.2, .5), (.4, -.3)])
def test_real_public_speech_pcm_supports_scaled_delayed_and_inverted_echo(delay, gain):
    with wave.open(str(Path(__file__).resolve().parents[1] / "assets/asr-smoke.wav"), "rb") as source:
        rate = source.getframerate()
        values = np.frombuffer(source.readframes(source.getnframes()), dtype="<i2").astype(np.float32) / 32768
        values = values.reshape(-1, source.getnchannels()).mean(axis=1)
    reference = np.interp(np.arange(round(len(values) / rate * RATE)) / RATE,
                          np.arange(len(values)) / rate, values).astype(np.float32)
    history = EchoAudioHistory()
    feed(history, reference, delay=delay, gain=gain)
    assert history.matches(3 + delay, 3.6 + delay)
