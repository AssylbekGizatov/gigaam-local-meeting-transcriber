from pathlib import Path
from contextlib import nullcontext
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import numpy as np

from gigaam_meeting import engine
from gigaam_meeting.contracts import Word
from gigaam_meeting import selftest


class FakeTensor:
    def __init__(self, values):
        self.values = np.asarray(values)

    def __len__(self):
        return len(self.values)

    def __getitem__(self, selection):
        return FakeTensor(self.values[selection])

    @property
    def shape(self):
        return self.values.shape

    def unsqueeze(self, axis):
        return FakeTensor(np.expand_dims(self.values, axis))


def fake_runtime(model):
    torch = types.ModuleType("torch")
    torch.from_numpy = FakeTensor
    torch.any = lambda tensor: np.any(tensor.values)
    torch.tensor = np.asarray
    torch.inference_mode = nullcontext
    torch.nn = types.SimpleNamespace(functional=types.SimpleNamespace(pad=lambda tensor, widths: FakeTensor(np.pad(tensor.values, widths))))
    audio = types.ModuleType("torchaudio.functional")
    audio.resample = lambda tensor, source, target: FakeTensor(tensor.values[::source // target])
    torchaudio = types.ModuleType("torchaudio")
    torchaudio.functional = audio
    return patch.dict(sys.modules, {"torch": torch, "torchaudio": torchaudio, "torchaudio.functional": audio})


class FakeModel:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.inputs = []

    def forward(self, wav, lengths):
        self.inputs.append(wav.values.copy())
        return "encoded", lengths

    def _decode(self, *args, **kwargs):
        return [next(self.responses)]


class EngineTests(unittest.TestCase):
    def test_official_verified_model_pins(self):
        self.assertEqual(engine.MODEL_REVISION, "2f8a57144e6ec3adfd32fe0484d9ea9913305bc8")
        self.assertEqual(engine.CODE_REVISION, "7447938d791c4f3e643386ee22c33777004293a5")
        self.assertEqual(engine.MODEL_FILES, {
            "config.json": "c830232c7d51688a630a221517b52585ab5ee57e1d3c21bcbae01759351d2653",
            "pytorch_model.bin": "e1db43873ec5e296f229572e06e2470fc157ac9f8d4aacabda295630b9b91728",
        })

    def test_missing_and_tampered_model_fail_closed(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(engine.EngineError):
                engine.verify_model(folder)
            for filename in engine.MODEL_FILES:
                (Path(folder) / filename).write_bytes(b"invalid")
            with self.assertRaises(engine.EngineError):
                engine.verify_model(folder)

    def test_source_and_frozen_model_roots(self):
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(engine.bundle_root(), Path(engine.__file__).resolve().parent.parent)
        with patch.object(sys, "frozen", True, create=True), patch.object(sys, "executable", str(Path.cwd() / "bundle" / "App.exe")):
            self.assertEqual(engine.bundle_root(), Path.cwd() / "bundle")

    def test_windows_have_no_ownership_gap(self):
        for total in [1, 16000, 24 * 16000, 24 * 16000 + 1, 300 * 16000]:
            windows = list(engine.windows(total))
            self.assertEqual(windows[0][2], 0)
            self.assertEqual(windows[-1][3], total / 16000)
            for first, second in zip(windows, windows[1:]):
                self.assertEqual(first[3], second[2])
                self.assertLessEqual(first[1] - first[0], 24 * 16000)

    def test_silence_does_not_load_model_or_write_audio(self):
        transcriber = engine.Transcriber("missing")
        with patch.object(transcriber, "load", side_effect=AssertionError("unexpected load")):
            self.assertEqual(transcriber.transcribe_words(np.zeros(20, dtype=np.float32), 16000), [])
            self.assertEqual(transcriber.transcribe(np.array([], dtype=np.float32), 16000), "")

    def test_invalid_input_rejected_before_load(self):
        transcriber = engine.Transcriber("missing")
        for samples, rate in [(np.zeros((5, 2)), 16000), (np.array([np.nan]), 16000), (np.ones(1), 0), (np.ones(1), 16000.0), (np.zeros(300 * 8000 + 1), 8000)]:
            with self.assertRaises(engine.EngineError):
                transcriber.transcribe_words(samples, rate)

    def test_transcribe_string_compatibility_retains_legitimate_repetition(self):
        transcriber = engine.Transcriber("missing")
        with patch.object(transcriber, "transcribe_words", return_value=[Word(0, .3, "да"), Word(.4, .7, "да")]):
            self.assertEqual(transcriber.transcribe(np.ones(16000), 16000), "да да")

    def test_native_word_path_resamples_and_returns_relative_ownership(self):
        transcriber = engine.Transcriber("missing")
        model = FakeModel([
            ("да да граница", [Word(0, .2, "да"), Word(.4, .6, "да"), Word(23.2, 23.8, "граница")]),
            ("граница", [Word(.2, .8, "граница")]),
        ])
        transcriber.model = model
        samples = np.full(round(24.1 * 48000), .2, dtype=np.float32)
        with fake_runtime(model), patch.object(transcriber, "load"):
            words = transcriber.transcribe_words(samples, 48000)
        self.assertEqual([word.text for word in words], ["да", "да", "граница"])
        self.assertEqual([clip.shape[-1] for clip in model.inputs], [24 * 16000, round(1.1 * 16000)])
        self.assertAlmostEqual(words[-1].start, 23.5)
        self.assertAlmostEqual(words[-1].end, 23.8)
        self.assertTrue(np.all(samples == np.float32(.2)))

    def test_missing_or_invalid_word_timestamps_fail_closed(self):
        for response in [("speech", []), ("speech", [Word(float("nan"), .2, "speech")]), ("speech", [Word(.3, .2, "speech")])]:
            transcriber = engine.Transcriber("missing")
            model = FakeModel([response])
            transcriber.model = model
            with fake_runtime(model), patch.object(transcriber, "load"), self.assertRaises(engine.EngineError):
                transcriber.transcribe_words(np.full(16000, .2, dtype=np.float32), 16000)

    def test_selftest_uses_only_exact_public_fixture(self):
        root = Path(engine.__file__).resolve().parent.parent
        samples, rate = selftest.read_fixture(root)
        self.assertEqual(rate, 16000)
        self.assertEqual(len(samples), 180640)
        self.assertEqual(samples.dtype, np.float32)
        with tempfile.TemporaryDirectory() as folder:
            asset = Path(folder) / selftest.FIXTURE_RELATIVE_PATH
            asset.parent.mkdir()
            asset.write_bytes(b"invalid fixture")
            with self.assertRaises(ValueError):
                selftest.read_fixture(Path(folder))

    def test_selftest_does_not_overwrite_existing_report(self):
        with tempfile.TemporaryDirectory() as folder:
            report_file = Path(folder) / "report.json"
            report_file.write_bytes(b"existing report")
            result = selftest.run_self_test(folder, report_file)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"]["code"], "report_already_exists")
            self.assertEqual(report_file.read_bytes(), b"existing report")

    def test_selftest_model_failure_is_safe_and_written(self):
        with tempfile.TemporaryDirectory() as folder:
            result = selftest.run_self_test(folder, Path(folder) / "report.json")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"], {"stage": "model", "type": "EngineError"})
            self.assertTrue((Path(folder) / "report.json").is_file())
            self.assertFalse(result["physical_capture_tested"])

    def test_public_inference_comparison_requires_true_recognized_words(self):
        fake = types.SimpleNamespace(transcribe_words=lambda samples, rate: [Word(0, .1, "incorrect")])
        with patch.object(selftest, "Transcriber", return_value=fake), patch.object(selftest, "read_fixture", return_value=(np.ones(16000), 16000)):
            with self.assertRaises(ValueError):
                selftest._public_inference(Path.cwd())


if __name__ == "__main__":
    unittest.main()
