"""Auditable build-only lazy-import patch; tensor inference remains unchanged.

Upstream eagerly imports soundfile for one optional AudioDataset path helper.
The desktop app feeds PCM tensors and does not expose that helper. Deferring the
import allows PyInstaller to omit libsndfile and its statically linked codecs.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import sys

ORIGINAL_SHA256 = "a6366d63c304b162202bf97a3c2da05a453741c475ab3bf160fc0428957282f9"


def patched(original):
    if original.count("import soundfile as sf\n") != 1 or original.count("            with sf.SoundFile(item) as f:\n") != 1:
        raise RuntimeError("Pinned GigaAM source no longer matches the reviewed lazy-import patch")
    return original.replace("import soundfile as sf\n", "").replace(
        "            with sf.SoundFile(item) as f:\n",
        "            import soundfile as sf  # Desktop build: optional path-only dependency.\n\n            with sf.SoundFile(item) as f:\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    path = Path(importlib.metadata.distribution("gigaam").locate_file("gigaam/utils.py"))
    original = path.read_text(encoding="utf-8")  # Normalize checkout CRLF before verification.
    original_hash = hashlib.sha256(original.encode("utf-8")).hexdigest()
    patch_dir = args.output / "gigaam-lazy-soundfile-patch"
    if original_hash != ORIGINAL_SHA256:
        backup = patch_dir / "utils.upstream.py.txt"
        saved = backup.read_text(encoding="utf-8") if backup.is_file() else ""
        if hashlib.sha256(saved.encode("utf-8")).hexdigest() != ORIGINAL_SHA256 or patched(saved) != original:
            raise RuntimeError("Unexpected GigaAM utils.py; review the patch against the pinned official source")
        original = saved  # A repeated build accepts only this exact prior patch.
        original_hash = ORIGINAL_SHA256
    modified = patched(original)
    path.write_text(modified, encoding="utf-8", newline="\n")
    patch_dir.mkdir(parents=True, exist_ok=True)
    (patch_dir / "utils.upstream.py.txt").write_text(original, encoding="utf-8", newline="\n")
    (patch_dir / "utils.patched.py.txt").write_text(modified, encoding="utf-8", newline="\n")
    (patch_dir / "change.patch").write_text("".join(difflib.unified_diff(original.splitlines(True), modified.splitlines(True),
        fromfile="gigaam/utils.py upstream", tofile="gigaam/utils.py desktop")), encoding="utf-8", newline="\n")
    (patch_dir / "provenance.json").write_text(json.dumps({
        "source": "https://github.com/salute-developers/GigaAM/blob/7447938d791c4f3e643386ee22c33777004293a5/gigaam/utils.py",
        "license": "MIT (preserved in licenses/gigaam)", "original_sha256_lf": original_hash,
        "patched_sha256_lf": hashlib.sha256(modified.encode("utf-8")).hexdigest(),
        "purpose": "Defer unused path-based AudioDataset soundfile import; omit libsndfile and bundled codecs from desktop runtime",
        "limitation": "Upstream optional file-based AudioDataset helper is not provided by the frozen desktop application"
    }, indent=2), encoding="utf-8")
    # A separate interpreter denies all soundfile imports, proving regular native
    # GigaAM tensor imports do not accidentally depend on that backend.
    subprocess.run([sys.executable, "-c", """
import importlib.abc, sys
class DenySoundFile(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'soundfile', '_soundfile', '_soundfile_data'}:
            raise ImportError('soundfile disabled in desktop runtime')
sys.meta_path.insert(0, DenySoundFile())
import torch, torchaudio, gigaam
from gigaam.utils import AudioDataset
assert AudioDataset._get_duration(torch.zeros(16000)) == 1.0
assert 'soundfile' not in sys.modules
print('Patched GigaAM imports and tensor duration work with soundfile denied')
"""], check=True)


if __name__ == "__main__":
    main()
