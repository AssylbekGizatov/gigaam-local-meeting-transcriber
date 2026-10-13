import os

import pytest

from gigaam_meeting.storage import validate_local_path
import gigaam_meeting.storage as storage


@pytest.mark.parametrize("path", [r"\\server\share\transcript.txt", "//server/share/transcript.txt", r"\\?\UNC\server\share\test"])
def test_unc_is_rejected_before_any_filesystem_access(path, monkeypatch):
    def forbidden_metadata(*args, **kwargs):
        raise AssertionError("UNC rejection must precede filesystem access")
    monkeypatch.setattr(storage.Path, "resolve", forbidden_metadata)
    with pytest.raises(ValueError, match="UNC"):
        validate_local_path(path)


def test_relative_path_is_rejected():
    with pytest.raises(ValueError):
        validate_local_path("relative/output.txt")


def test_validation_creates_nothing(tmp_path):
    destination = tmp_path / "does-not-exist" / "output.txt"
    assert validate_local_path(destination) == destination.resolve()
    assert not destination.parent.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows mapped drive policy")
def test_mapped_network_drive_is_rejected_before_resolve(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "_windows_drive_type", lambda anchor: 4)
    def forbidden_metadata(*args, **kwargs):
        raise AssertionError("Mapped drive rejection must precede filesystem access")
    monkeypatch.setattr(storage.Path, "resolve", forbidden_metadata)
    with pytest.raises(ValueError, match="сетевые"):
        validate_local_path(tmp_path / "output.txt")
