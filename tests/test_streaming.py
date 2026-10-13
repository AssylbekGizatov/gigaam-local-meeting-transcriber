import threading
import time
import unittest

import numpy as np

from gigaam_meeting.contracts import AudioBlock, Word
from gigaam_meeting.streaming import StreamProcessor
from gigaam_meeting.selftest import _bounded_streaming


class RunModel:
    """Deterministic words represented by nonzero, constant-valued PCM runs."""
    def __init__(self):
        self.calls = []
        self.active = 0
        self.peak = 0

    def transcribe_words(self, samples, rate):
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.calls.append((samples.copy(), rate))
        try:
            cuts = np.flatnonzero(np.diff(samples) != 0) + 1
            boundaries = np.r_[0, cuts, len(samples)]
            return [Word(a / rate, b / rate, f"word{int(round(float(samples[a]) * 100))}")
                    for a, b in zip(boundaries, boundaries[1:]) if samples[a] != 0]
        finally:
            self.active -= 1


def block(source="microphone", start=0, seconds=1, value=0.25, rate=16000, discontinuity=False):
    return AudioBlock(source, np.full(round(seconds * rate), value, dtype=np.float32), rate, start, discontinuity)


class StreamingTests(unittest.TestCase):
    def make_processor(self, model=None, **kwargs):
        self.model = model or RunModel()
        self.segments = []
        self.statuses = []
        processor = StreamProcessor(self.model, self.segments.append, lambda kind, data: self.statuses.append((kind, data)), **kwargs)
        self.addCleanup(processor.stop)
        processor.start()
        return processor

    def test_two_simultaneous_sources_are_not_mixed_or_diarized(self):
        processor = self.make_processor()
        processor.feed(block("microphone", rate=48000, value=0.11))
        processor.feed(block("system", rate=44100, value=0.22))
        processor.stop()
        self.assertEqual({s.source: s.text for s in self.segments}, {"microphone": "word11", "system": "word22"})
        self.assertEqual({rate for _, rate in self.model.calls}, {48000, 44100})
        self.assertEqual(self.model.peak, 1)
        self.assertEqual(processor.pending_seconds, 0)

    def test_overlap_owns_each_word_once_and_keeps_real_repeated_words(self):
        processor = self.make_processor()
        # Word 7.2..7.9 crosses the ownership seam but is wholly inside
        # both inputs. Three separate equal words remain three equal words.
        pcm = np.zeros(15 * 16000, dtype=np.float32)
        for a, b, value in [(1, 1.5, .1), (3, 3.5, .1), (7.2, 7.9, .1), (9, 9.8, .2), (14, 14.8, .3)]:
            pcm[round(a * 16000):round(b * 16000)] = value
        for index in range(15):
            self.assertTrue(processor.feed(AudioBlock("microphone", pcm[index*16000:(index+1)*16000], 16000, float(index))))
        processor.stop()
        words = " ".join(s.text for s in self.segments).split()
        self.assertEqual(words, ["word10", "word10", "word10", "word20", "word30"])
        self.assertTrue(any(np.count_nonzero(values == np.float32(.1)) >= .7 * 16000 for values, _ in self.model.calls))

    def test_seam_timestamp_jitter_does_not_duplicate_same_acoustic_word(self):
        class JitterModel:
            calls = 0
            def transcribe_words(self, pcm, rate):
                self.calls += 1
                if self.calls == 1:
                    return [Word(7.1, 7.88, "да")]  # midpoint7.49, owned by window1
                if self.calls == 2:
                    # Same acoustic word shifts to midpoint7.51. Genuine
                    # adjacent repetition7.96..8.3 must remain untouched.
                    return [Word(.12, .9, "да"), Word(.96, 1.3, "да")]
                return []
        processor = self.make_processor(JitterModel())
        processor.feed(block(seconds=8))
        processor.feed(block(start=8, seconds=1))
        processor.stop()
        self.assertEqual(" ".join(s.text for s in self.segments), "да да")
        self.assertEqual(len(self.segments), 2)
        self.assertAlmostEqual(self.segments[1].start, 7.96)

    def test_seam_guard_never_deduplicates_another_source(self):
        class SameWords:
            def transcribe_words(self, pcm, rate):
                return [Word(.1, .4, "да")]
        processor = self.make_processor(SameWords())
        processor.feed(block("microphone"))
        processor.feed(block("system"))
        processor.stop()
        self.assertEqual([(s.source, s.text) for s in self.segments], [("microphone", "да"), ("system", "да")])

    def test_real_fixture_displacement_aligns_context_without_interval_intersection(self):
        class ShiftedContext:
            calls = 0
            def transcribe_words(self, pcm, rate):
                self.calls += 1
                if self.calls == 1:
                    return [Word(7.08, 7.16, "на"), Word(7.2, 7.48, "песни"), Word(7.56, 7.72, "пись")]
                return [Word(.32074766, .40093458, "на"), Word(.48112149, .72168224, "песни"), Word(.84196262, 1.20280374, "грешные")]
        processor = self.make_processor(ShiftedContext())
        processor.feed(block(seconds=8))
        processor.feed(block(start=8, seconds=1))
        processor.stop()
        self.assertEqual(" ".join(s.text for s in self.segments), "на песни грешные")

    def test_full_unconfirmed_context_preserves_real_adjacent_repeated_word(self):
        class RepeatingContext:
            calls = 0
            def transcribe_words(self, pcm, rate):
                self.calls += 1
                if self.calls == 1:
                    return [Word(7.08, 7.16, "на"), Word(7.2, 7.48, "песни"), Word(7.58, 7.84, "песни")]
                return [Word(.32074766, .40093458, "на"), Word(.48112149, .72168224, "песни"), Word(.86, 1.1, "песни")]
        processor = self.make_processor(RepeatingContext())
        processor.feed(block(seconds=8))
        processor.feed(block(start=8, seconds=1))
        processor.stop()
        self.assertEqual(" ".join(s.text for s in self.segments), "на песни песни")

    def test_isolated_same_word_without_strong_time_evidence_is_not_collapsed(self):
        class AmbiguousRepeat:
            calls = 0
            def transcribe_words(self, pcm, rate):
                self.calls += 1
                return [Word(7.2, 7.4, "да")] if self.calls == 1 else [Word(.6, .8, "да")]
        processor = self.make_processor(AmbiguousRepeat())
        processor.feed(block(seconds=8))
        processor.feed(block(start=8, seconds=1))
        processor.stop()
        self.assertEqual(" ".join(s.text for s in self.segments), "да да")

    def test_fast_new_phrase_without_pcm_overlap_keeps_repeated_context(self):
        class NewPhrase:
            calls = 0
            def transcribe_words(self, pcm, rate):
                self.calls += 1
                return ([Word(.1, .2, "на"), Word(.3, .4, "песни")] if self.calls == 1
                        else [Word(0, .1, "на"), Word(.2, .3, "песни")])
        processor = self.make_processor(NewPhrase())
        processor.feed(block(seconds=.4))
        processor.pause_flush()
        processor.feed(block(start=.45, seconds=.4))
        processor.stop()
        self.assertEqual(" ".join(s.text for s in self.segments), "на песни на песни")

    def test_pause_and_final_flush_preserve_source_clock_gaps(self):
        processor = self.make_processor()
        processor.feed(block(start=1, seconds=.4, value=.1))
        processor.pause_flush()
        processor.feed(block(start=20, seconds=.4, value=.2))
        processor.stop()
        self.assertEqual([s.text for s in self.segments], ["word10", "word20"])
        self.assertAlmostEqual(self.segments[0].start, 1)
        self.assertAlmostEqual(self.segments[0].end, 1.4)
        self.assertAlmostEqual(self.segments[1].start, 20)
        self.assertAlmostEqual(self.segments[1].end, 20.4)
        self.assertFalse(processor.feed(block(start=21)))
        processor.stop()

    def test_pcm_pause_flush_uses_audio_not_callback_wall_clock(self):
        processor = self.make_processor()
        processor.feed(block(seconds=.4))
        for index in range(14):
            processor.feed(block(start=.4 + index * .1, seconds=.1, value=0))
        deadline = time.monotonic() + 2
        while not self.segments and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual(len(self.segments), 1)
        processor.stop()
        self.assertEqual(len(self.segments), 1)

    def test_no_callback_short_phrase_flushes_without_pause_or_stop(self):
        processor = self.make_processor(idle_timeout_seconds=.05)
        processor.feed(block("system", seconds=.4))
        deadline = time.monotonic() + 2
        while not any(kind == "progress" for kind, _ in self.statuses) and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertEqual([s.text for s in self.segments], ["word25"])
        self.assertEqual(processor.pending_seconds, 0)
        self.assertFalse(any(kind == "discontinuity" for kind, _ in self.statuses))
        processor.stop()
        self.assertEqual(len(self.segments), 1)

    def test_progress_retired_after_inference_updates_lag_without_new_audio(self):
        gate = threading.Event()
        entered = threading.Event()
        model = RunModel()
        base = model.transcribe_words
        def blocked(samples, rate):
            entered.set()
            gate.wait(2)
            return base(samples, rate)
        model.transcribe_words = blocked
        processor = self.make_processor(model)
        self.addCleanup(gate.set)
        processor.feed(block("system", seconds=.4))
        processor.pause_flush()
        self.assertTrue(entered.wait(1))
        self.assertAlmostEqual(processor.pending_seconds, .4)
        self.assertFalse(any(kind == "progress" for kind, _ in self.statuses))
        gate.set()
        processor.stop()
        events = [data for kind, data in self.statuses if kind == "progress"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[-1]["pending_seconds"], 0)
        processor.stop()
        self.assertEqual(len(self.segments), 1)

    def test_quiet_speech_is_kept(self):
        processor = self.make_processor()
        processor.feed(block(seconds=1.5, value=.000002))
        processor.stop()
        self.assertEqual(len(self.segments), 1)
        self.assertEqual(sum(np.count_nonzero(samples) for samples, _ in self.model.calls), 24000)

    def test_input_buffer_is_copied_before_capture_reuses_it(self):
        processor = self.make_processor()
        original = np.full((16000, 2), .25, dtype=np.float32)
        processor.feed(AudioBlock("system", original, 16000, 0))
        original[:] = 0  # Hardware callbacks reuse their sample buffers.
        processor.stop()
        self.assertEqual([s.text for s in self.segments], ["word25"])
        self.assertTrue(np.all(self.model.calls[0][0] == .25))

    def test_duplicate_and_backward_timestamps_never_repeat_pcm(self):
        processor = self.make_processor()
        self.assertTrue(processor.feed(block(seconds=.5)))
        self.assertFalse(processor.feed(block(seconds=.5)))
        self.assertFalse(processor.feed(block(start=.25, seconds=.5)))
        self.assertTrue(processor.feed(block(start=.5, seconds=.5, value=.3)))
        processor.stop()
        self.assertEqual(sum(len(samples) for samples, _ in self.model.calls), 16000)
        self.assertEqual([s.text for s in self.segments], ["word25 word30"])
        self.assertEqual(sum(kind == "capture_timestamp" for kind, _ in self.statuses), 2)

    def test_gaps_rate_change_and_device_discontinuity_flush(self):
        processor = self.make_processor()
        processor.feed(block(start=0, seconds=.2))
        processor.feed(block(start=1, seconds=.2, rate=48000))
        processor.feed(block(start=1.2, seconds=.2, rate=48000, discontinuity=True))
        processor.stop()
        self.assertEqual(len(self.segments), 3)
        self.assertEqual([round(s.start, 2) for s in self.segments], [0, 1, 1.2])
        self.assertEqual(sum(kind == "discontinuity" for kind, _ in self.statuses), 2)

    def test_overload_is_bounded_visible_and_does_not_block_capture(self):
        gate = threading.Event()
        entered = threading.Event()
        model = RunModel()
        base = model.transcribe_words
        def slow(samples, rate):
            entered.set()
            gate.wait(3)
            return base(samples, rate)
        model.transcribe_words = slow
        processor = self.make_processor(model, max_pending_seconds=18)
        self.addCleanup(gate.set)
        processor.feed(block(seconds=8))
        self.assertTrue(entered.wait(1))
        started = time.monotonic()
        for index in range(1, 20):
            processor.feed(block(start=index*8, seconds=8))
            self.assertLessEqual(processor.pending_seconds, 18)
        self.assertLess(time.monotonic() - started, 1)
        self.assertTrue(any(kind == "overload" for kind, _ in self.statuses))
        self.assertEqual(sum(kind == "overload" for kind, _ in self.statuses), 1)
        gate.set()
        processor.stop()
        self.assertEqual(processor.pending_seconds, 0)

    def test_failed_inference_does_not_kill_worker_or_disclose_speech(self):
        model = RunModel()
        base = model.transcribe_words
        counter = 0
        def flaky(samples, rate):
            nonlocal counter
            counter += 1
            if counter == 1:
                raise RuntimeError("private conversation in unsafe dependency error")
            return base(samples, rate)
        model.transcribe_words = flaky
        processor = self.make_processor(model)
        processor.feed(block())
        processor.pause_flush()
        processor.feed(block(start=10, value=.3))
        processor.stop()
        self.assertEqual([s.text for s in self.segments], ["word30"])
        self.assertTrue(any(kind == "inference_failed" for kind, _ in self.statuses))
        self.assertNotIn("private conversation", repr(self.statuses))

    def test_long_session_keeps_no_per_session_pcm_history(self):
        model = RunModel()
        processor = self.make_processor(model)
        # 1,000 independent phrases, processed before the next arrives. The
        # processor's PCM budget is independent of total session duration.
        for index in range(1000):
            processor.feed(block(start=index * 2, seconds=.02))
            processor.pause_flush()
            deadline = time.monotonic() + 1
            while processor.pending_seconds and time.monotonic() < deadline:
                time.sleep(.0005)
            self.assertEqual(processor.pending_seconds, 0)
            self.assertEqual(sum(state.frames for state in processor._states.values()), 0)
        processor.stop()
        self.assertEqual(len(self.segments), 1000)

    def test_two_hour_continuous_fixture_clock_has_bounded_memory(self):
        result = _bounded_streaming()
        self.assertEqual(result["simulated_seconds"], 7200)
        self.assertGreater(result["inference_windows"], 1000)
        self.assertLessEqual(result["maximum_pending_seconds"], result["configured_bound_seconds"])
        self.assertEqual(result["peak_parallel_inference"], 1)
        self.assertEqual(result["pending_after_stop_seconds"], 0)

    def test_invalid_audio_and_bad_model_timestamps_are_visible(self):
        processor = self.make_processor()
        self.assertFalse(processor.feed(AudioBlock("microphone", np.array([np.nan]), 16000, 0)))
        self.assertFalse(processor.feed(block(seconds=9)))
        self.assertFalse(processor.feed(block(start=-1)))
        self.model.transcribe_words = lambda samples, rate: [Word(0, 1000, "unsafe")]
        processor.feed(block())
        processor.stop()
        self.assertEqual(self.segments, [])
        self.assertTrue(any(kind == "inference_failed" for kind, _ in self.statuses))


if __name__ == "__main__":
    unittest.main()
