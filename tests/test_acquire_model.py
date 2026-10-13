"""Build-only acquisition: correct source, integrity and failed-download cleanup."""
import hashlib
import importlib.util
import io
from pathlib import Path
from urllib.error import HTTPError

import pytest

spec = importlib.util.spec_from_file_location("acquire_model_tool", Path(__file__).resolve().parents[1] / "build/acquire_model.py")
acquisition = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acquisition)


def test_license_uses_pinned_code_source_not_missing_model_file():
    assert acquisition.artifact_url("LICENSE") == (
        "https://raw.githubusercontent.com/salute-developers/GigaAM/"
        "7447938d791c4f3e643386ee22c33777004293a5/LICENSE"
    )
    assert acquisition.REVISION in acquisition.artifact_url("pytorch_model.bin")


def test_failed_download_can_retry_without_leftover_or_overwrite(tmp_path, monkeypatch):
    content = b"verified public build fixture"
    monkeypatch.setattr(acquisition, "FILES", {"config.json": hashlib.sha256(content).hexdigest()})

    def unavailable(*args, **kwargs):
        raise HTTPError("https://example.invalid", 404, "Unavailable", {}, None)

    monkeypatch.setattr(acquisition.urllib.request, "urlopen", unavailable)
    with pytest.raises(HTTPError):
        acquisition.acquire(tmp_path)
    assert list(tmp_path.iterdir()) == []
    monkeypatch.setattr(acquisition.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(content))
    acquisition.acquire(tmp_path)
    assert (tmp_path / "config.json").read_bytes() == content


def test_wrong_digest_never_installs_or_overwrites(tmp_path, monkeypatch):
    monkeypatch.setattr(acquisition, "FILES", {"config.json": "0" * 64})
    monkeypatch.setattr(acquisition.urllib.request, "urlopen", lambda *args, **kwargs: io.BytesIO(b"incorrect"))
    with pytest.raises(RuntimeError, match="Downloaded"):
        acquisition.acquire(tmp_path)
    assert list(tmp_path.iterdir()) == []
    (tmp_path / "config.json").write_bytes(b"existing")
    with pytest.raises(RuntimeError, match="Existing"):
        acquisition.acquire(tmp_path)
    assert (tmp_path / "config.json").read_bytes() == b"existing"
