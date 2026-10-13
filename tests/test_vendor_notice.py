import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("vendor_notice_tool", Path(__file__).resolve().parents[1] / "build/prepare_bundle.py")
bundle_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle_tool)


def test_full_pinned_microsoft_terms_packaged_without_network(tmp_path, monkeypatch):
    def no_network(*args, **kwargs):
        raise AssertionError("Vendor notice must come from the verified source artifact")
    monkeypatch.setattr(bundle_tool.urllib.request, "urlopen", no_network)
    result = bundle_tool.collect_microsoft_notice(tmp_path)
    target = tmp_path / "licenses" / result["file"]
    assert target.stat().st_size == 39644
    assert bundle_tool.digest(target) == result["sha256"] == bundle_tool.MICROSOFT_NOTICE_SHA256
    assert result["url"] == bundle_tool.MICROSOFT_NOTICE_URL


def test_corrupt_vendor_terms_fail_without_silent_download(tmp_path, monkeypatch):
    monkeypatch.setattr(bundle_tool, "ROOT", tmp_path)
    cached = tmp_path / "assets/licenses/Microsoft-VC-Runtime-2015-2022-License.docx"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"not the vendor notice")
    with pytest.raises(RuntimeError, match="missing or changed"):
        bundle_tool.collect_microsoft_notice(tmp_path / "bundle")
    assert cached.read_bytes() == b"not the vendor notice"
    assert not (tmp_path / "bundle").exists()
