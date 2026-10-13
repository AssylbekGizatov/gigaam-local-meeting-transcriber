"""Build-time HTTPS acquisition only. Never imported by the desktop application."""
import argparse
import hashlib
from pathlib import Path
import shutil
import urllib.request

REVISION = "2f8a57144e6ec3adfd32fe0484d9ea9913305bc8"
FILES = {
    "config.json": "c830232c7d51688a630a221517b52585ab5ee57e1d3c21bcbae01759351d2653",
    "pytorch_model.bin": "e1db43873ec5e296f229572e06e2470fc157ac9f8d4aacabda295630b9b91728",
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", type=Path, default=Path("models/gigaam"))
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=True)
    for name in (*FILES, "LICENSE"):
        target = args.directory / name
        if not target.exists():
            partial = target.with_suffix(target.suffix + ".partial")
            with urllib.request.urlopen(f"https://huggingface.co/ai-sage/GigaAM-Multilingual/resolve/{REVISION}/{name}", timeout=120) as source:
                with partial.open("xb") as out:
                    shutil.copyfileobj(source, out, length=1024 * 1024)
            partial.rename(target)
        if name in FILES:
            with target.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            if digest != FILES[name]:
                raise RuntimeError(f"Existing/downloaded {name} failed SHA256; never overwrite it silently")


if __name__ == "__main__":
    main()
