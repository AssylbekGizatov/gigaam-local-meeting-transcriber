"""Publication tests use fabricated values; no actual credential is accessed."""
import hashlib
import importlib.util
import json
from pathlib import Path
from urllib.parse import quote
import zipfile

import pytest

SPEC = importlib.util.spec_from_file_location("publication_source_tool", Path(__file__).resolve().parents[1] / "build/source_package.py")
publication = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(publication)


def test_source_selection_never_walks_generated_or_private_trees(tmp_path):
    approved = ["gigaam_meeting/sample.py", "tests/test_sample.py", "build/package.py", "build/audit.ps1",
                "docs/REPORT.md", "docs/verification/runtime.json", "docs/verification/unit.xml",
                "docs/verification/gigaam-lazy-soundfile-patch/utils.upstream.py.txt",
                "docs/verification/gigaam-lazy-soundfile-patch/change.patch",
                ".github/workflows/windows.yml"]
    private = [".env", "models/config.json", "dist/app.py", ".venv/secret.py", "build/publication/payload.json",
               "build/reports/source.py", "build/license-staging/README.md", "docs/verification/raw.log", "docs/verification/raw.txt",
               "docs/verification/other/code.py.txt",
               "docs/unapproved-folder/notes.md", "audio-private/microphone.wav"]
    for relative in approved + private:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public test placeholder", encoding="utf-8")
    selected = {path.relative_to(tmp_path).as_posix() for path in publication.source_paths(tmp_path)}
    assert set(approved) <= selected
    assert not set(private) & selected


@pytest.mark.parametrize("rule,content", [
    ("github_credential", "github_pat_" + "A" * 40),
    ("private_key", "-----BEGIN " + "PRIVATE KEY-----"),
    ("windows_user_path", "C:" + "\\" + "Users" + "\\" + "PrivateOwner"),
    ("unix_user_path", "/" + "home" + "/private-owner/project"),
    ("meeting_url", "https://" + "teams.live.com" + "/meet/123456789?p=private"),
    ("signed_url", "https://example.com/resource?" + "X-Amz-Signature=" + "b" * 40),
    ("bearer_credential", "Bearer " + "B" * 40),
    ("literal_secret_assignment", 'API_KEY = "' + "C" * 40 + '"'),
], ids=["github_credential", "private_key", "windows_user_path", "unix_user_path",
        "meeting_url", "signed_url", "bearer_credential", "literal_secret_assignment"])
def test_private_pattern_reports_location_without_value(rule, content):
    findings = publication.scan_text("docs/report.json", "public heading\n" + content)
    assert any(item["rule"] == rule and item["line"] == 2 for item in findings)
    assert all(set(item) == {"path", "line", "rule"} for item in findings)
    assert content not in json.dumps(findings)


def test_percent_encoding_cannot_hide_signed_url():
    url = "https://example.com/file?" + "X-Amz-Signature=" + "d" * 40
    assert any(item["rule"] == "signed_url" for item in publication.scan_text("docs/report.md", quote(url, safe="")))


def test_public_model_hash_and_source_link_are_not_credentials():
    content = 'MODEL_SHA256 = "' + "a" * 64 + '"\nhttps://github.com/salute-developers/GigaAM'
    assert publication.scan_text("gigaam_meeting/engine.py", content) == []


def source_item():
    content = "Бүгін қазақша. Public source.\n"
    return {"path": "README.md", "encoding": "utf-8", "content": content,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()}


def test_source_zip_exact_bytes_checksum_and_no_overwrite(tmp_path):
    item = source_item()
    archive, checksum = publication.package_source([item], tmp_path / "source.zip")
    before = archive.read_bytes()
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.namelist() == ["gigaam-local-meeting-transcriber/README.md"]
        assert zipped.read(zipped.namelist()[0]) == item["content"].encode("utf-8")
    assert checksum.read_text(encoding="ascii").startswith(hashlib.sha256(before).hexdigest())
    with pytest.raises(FileExistsError):
        publication.package_source([item], archive)
    assert archive.read_bytes() == before


def test_competing_exclusive_writer_is_not_deleted(tmp_path, monkeypatch):
    archive = tmp_path / "source.zip"

    def competing_create(*args, **kwargs):
        archive.write_bytes(b"another writer's file")
        raise FileExistsError("simulated concurrent creation")

    monkeypatch.setattr(publication.zipfile, "ZipFile", competing_create)
    with pytest.raises(FileExistsError):
        publication.package_source([source_item()], archive)
    assert archive.read_bytes() == b"another writer's file"


def test_json_audit_or_payload_never_replaces_existing_file(tmp_path):
    path = tmp_path / "report.json"
    path.write_text("original", encoding="utf-8")
    with pytest.raises(FileExistsError):
        publication.write_json_exclusive(path, {"status": "passed"})
    assert path.read_text(encoding="utf-8") == "original"
