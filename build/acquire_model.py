"""Build-time HTTPS acquisition only. Never imported by the desktop application."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import urllib.request

REVISION = "2f8a57144e6ec3adfd32fe0484d9ea9913305bc8"
FILES = {
    "config.json": "c830232c7d51688a630a221517b52585ab5ee57e1d3c21bcbae01759351d2653",
    "pytorch_model.bin": "e1db43873ec5e296f229572e06e2470fc157ac9f8d4aacabda295630b9b91728",
    "LICENSE": "f00de6715714c7a63d08639cdbfaa40224eefc407302614bd19f1a8b98c875aa",
}
CODE_REVISION = "7447938d791c4f3e643386ee22c33777004293a5"


def artifact_url(name):
    # The pinned model card declares MIT, but its repository has no LICENSE file.
    # Include the upstream GigaAM full terms from the same pinned code we package.
    if name == "LICENSE":
        return f"https://raw.githubusercontent.com/salute-developers/GigaAM/{CODE_REVISION}/LICENSE"
    return f"https://huggingface.co/ai-sage/GigaAM-Multilingual/resolve/{REVISION}/{name}"


def acquire(directory):
    directory.mkdir(parents=True, exist_ok=True)
    for name, expected in FILES.items():
        target = directory / name
        if not target.exists():
            with tempfile.TemporaryDirectory(prefix=".acquire-", dir=directory) as scratch:
                partial = Path(scratch) / name
                with urllib.request.urlopen(artifact_url(name), timeout=120) as source:
                    with partial.open("xb") as out:
                        shutil.copyfileobj(source, out, length=1024 * 1024)
                with partial.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest != expected:
                    raise RuntimeError(f"Downloaded {name} failed SHA256")
                # Windows rename refuses an existing destination, including a race.
                partial.rename(target)
        with target.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if digest != expected:
            raise RuntimeError(f"Existing {name} failed SHA256; never overwrite it silently")
    provenance = directory / "provenance.json"
    record = {"backend": "gigaam", "repository": "ai-sage/GigaAM-Multilingual", "revision": REVISION,
              "code_revision": CODE_REVISION, "license": "MIT", "sha256": FILES,
              "model_card": f"https://huggingface.co/ai-sage/GigaAM-Multilingual/blob/{REVISION}/README.md",
              "artifact_sources": {name: artifact_url(name) for name in FILES}}
    if provenance.exists():
        current = json.loads(provenance.read_text(encoding="utf-8"))
        if any(current.get(key) != record[key] for key in ("repository", "revision", "code_revision", "sha256")):
            raise RuntimeError("Existing model provenance differs from verified pins; never overwrite it silently")
    else:
        with provenance.open("x", encoding="utf-8") as output:
            json.dump(record, output, indent=2)
            output.write("\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path("models/gigaam"))
    args = parser.parse_args()
    acquire(args.directory)


if __name__ == "__main__":
    main()
