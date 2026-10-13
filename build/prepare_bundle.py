"""Build-time only: preserve notices, acquisition evidence and pinned test data."""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata as metadata
import json
from pathlib import Path
import platform
import shutil
import sys
import tkinter
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
CODE_REVISION = "7447938d791c4f3e643386ee22c33777004293a5"
PAWP_REVISION = "8b68e78c31dd1e12e9c603901b8cd0d9d6510b2a"
PAWP_BASE = f"https://raw.githubusercontent.com/s0d3s/PyAudioWPatch/{PAWP_REVISION}/"
SAMPLE_URL = "https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/example.wav"
SAMPLE_SHA256 = "d8aaaa18a5098d7c6de0595ae7ac1e64cacd0d4022af3595213bdaf23be77e69"

# The pinned Windows SentencePiece wheel omits all license files. Preserve the
# exact matching upstream license plus every vendored third-party license.
SENTENCEPIECE_LICENSES = {
    "LICENSE": "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30",
    "third_party/absl/LICENSE": "d3e2f59e1d71176dfdb555ece6a41f7a5aa0f52ff21211010ace314f57695f6b",
    "third_party/darts_clone/LICENSE": "155f59997298ee336602c49f9c1110f268ac394ca2197eb02647a3555935ad52",
    "third_party/esaxx/LICENSE": "7c28553d1d3312d65fe309f76a22ebaf33a3d76c8c1e3b98a88ee4654ecb53db",
    "third_party/protobuf-lite/LICENSE": "6e5e117324afd944dcf67f36cf329843bc1a92229a8cd9bb573d7a83130fea7d",
}


def digest(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def fetch(url, target, expected=None, limit=60_000_000):
    target.parent.mkdir(parents=True, exist_ok=True)
    if expected and target.is_file() and digest(target) == expected:
        return {"url": url, "sha256": expected, "bytes": target.stat().st_size, "file": target.name,
                "verification": "cached file matched pre-pinned SHA256"}
    with urllib.request.urlopen(url, timeout=120) as response, target.open("wb") as output:
        count = 0
        while block := response.read(1024 * 1024):
            count += len(block)
            if count > limit:
                raise RuntimeError(f"Download too large: {url}")
            output.write(block)
    sha = digest(target)
    if expected and sha != expected:
        target.unlink()
        raise RuntimeError(f"Checksum mismatch: {url}")
    return {"url": url, "sha256": sha, "bytes": target.stat().st_size, "file": target.name,
            "verification": "pre-pinned SHA256" if expected else "HTTPS download; SHA256 recorded at build"}


def notice_name(path):
    name = Path(path).name.lower()
    return any(word in name for word in ("license", "licence", "copying", "notice", "copyright"))


def collect_licenses(bundle):
    licenses = bundle / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    packages = []
    for dist in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = dist.metadata["Name"]
        destination = licenses / name
        destination.mkdir(exist_ok=True)
        notices = []
        for relative in dist.files or []:
            original = Path(dist.locate_file(relative))
            if not notice_name(relative) or not original.is_file() or original.stat().st_size > 3_000_000:
                continue
            # Do not extract executable files whose parent happens to be named "licenses".
            if original.suffix.lower() in {".dll", ".exe", ".pyd", ".py", ".pyc"}:
                continue
            filename = hashlib.sha256(str(relative).encode()).hexdigest()[:8] + "-" + original.name
            copied = destination / filename
            shutil.copy2(original, copied)
            notices.append({"original": str(relative), "file": str(copied.relative_to(bundle)), "sha256": digest(copied)})
        # The antlr runtime sdist omits its BSD license file. Preserve upstream text.
        if name.lower().replace("_", "-") == "antlr4-python3-runtime" and not notices:
            item = fetch("https://raw.githubusercontent.com/antlr/antlr4/4.9.3/LICENSE.txt", destination / "LICENSE.txt")
            notices.append(item)
        if name.lower().replace("_", "-") == "sentencepiece":
            if dist.version != "0.2.1":
                raise RuntimeError("Review SentencePiece notice pins before changing its version")
            for relative, expected in SENTENCEPIECE_LICENSES.items():
                target = destination / (relative.replace("/", "-") + ".txt")
                item = fetch("https://raw.githubusercontent.com/google/sentencepiece/v0.2.1/" + relative,
                             target, expected, limit=100_000)
                item["file"] = str(target.relative_to(bundle))
                notices.append(item)
        if name.lower().replace("_", "-") == "pyaudiowpatch":
            if dist.version != "0.2.12.9":
                raise RuntimeError("Review PyAudioWPatch native provenance before changing its version")
            # The fork's wheel contains a short Apache notice, which is not the
            # full license. Its native extension also embeds upstream MIT code.
            for url, filename, expected in (
                ("https://www.apache.org/licenses/LICENSE-2.0.txt", "Apache-2.0-full.txt", "cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30"),
                (PAWP_BASE + "portaudio_v19/LICENSE.txt", "PortAudio-fork-LICENSE.txt", "ec52a1952d701f94e5135719a47376da4ee0b4a0201f1cafb49f61db6480ac3d"),
                (PAWP_BASE + "setup.py", "PyAudio-upstream-MIT-source.py.txt", "36c1605e33928e7f40468c6efd6a98ce8ce31afabe59a772dfe847e38f67e959"),
            ):
                target = destination / filename
                item = fetch(url, target, expected, limit=100_000)
                item["file"] = str(target.relative_to(bundle))
                notices.append(item)
            upstream = destination / "PyAudio-upstream-MIT-source.py.txt"
            text = ast.get_docstring(ast.parse(upstream.read_text(encoding="utf-8")))
            if not text or "Copyright (c) 2006 Hubert Pham" not in text or "Permission is hereby granted" not in text:
                raise RuntimeError("Full upstream PyAudio MIT notice is missing")
            target = destination / "PyAudio-original-MIT.txt"
            target.write_text(text + "\n", encoding="utf-8")
            notices.append({"file": str(target.relative_to(bundle)), "sha256": digest(target),
                            "source": PAWP_BASE + "setup.py", "method": "complete upstream module docstring; no source code executed"})
        if not notices:
            raise RuntimeError(f"No full license text found for {name}; do not publish an incomplete bundle")
        packages.append({"name": name, "version": dist.version, "license_expression": dist.metadata.get("License-Expression"),
                         "license_metadata": dist.metadata.get("License"), "home_page": dist.metadata.get("Home-page"),
                         "project_urls": dist.metadata.get_all("Project-URL") or [], "notices": notices})
    return packages


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--reports", type=Path, required=True)
    args = parser.parse_args()
    bundle = args.bundle.resolve()
    notices = bundle / "licenses"
    packages = collect_licenses(bundle)
    provenance = bundle / "provenance"
    shutil.copytree(args.reports, provenance, dirs_exist_ok=True)
    shutil.copy2(ROOT / "LICENSE", bundle / "LICENSE.txt")
    shutil.copy2(ROOT / "README.md", bundle / "README.md")
    acquired = []
    font_notice = notices / "DejaVuSans" / "LICENSE.txt"
    font_notice.parent.mkdir(exist_ok=True)
    shutil.copy2(ROOT / "fonts" / "LICENSE.txt", font_notice)
    acquired.append({"component": "DejaVuSans", "file": str(font_notice.relative_to(bundle)),
                     "license_sha256": digest(font_notice),
                     "font_sha256": digest(ROOT / "fonts" / "DejaVuSans.ttf"),
                     "font_source": "https://raw.githubusercontent.com/matplotlib/matplotlib/v3.10.0/lib/matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf",
                     "license_source": "https://raw.githubusercontent.com/dejavu-fonts/dejavu-fonts/version_2_37/LICENSE"})
    python_version = platform.python_version()
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if python_license.is_file():
        shutil.copy2(python_license, notices / "Python-LICENSE.txt")
    else:
        acquired.append(fetch(f"https://raw.githubusercontent.com/python/cpython/v{python_version}/LICENSE",
                              notices / "Python-LICENSE.txt", limit=100_000))
    microsoft = fetch(
        "https://visualstudio.microsoft.com/wp-content/uploads/2021/09/Visual-C-Runtime-2015-2022-License-1.docx",
        notices / "Microsoft-VC-Runtime-2015-2022-License.docx",
        "f1e3d56ceb2ad68aae0711b910375009e651ac5530fa0760f0dea6e81e54fae1",
        limit=1_000_000)
    microsoft["source_page"] = "https://visualstudio.microsoft.com/license-terms/vs2022-cruntime/"
    acquired.append(microsoft)
    acquired.append(fetch(
        "https://huggingface.co/ai-sage/GigaAM-Multilingual/resolve/2f8a57144e6ec3adfd32fe0484d9ea9913305bc8/README.md",
        notices / "GigaAM-model-card.md"))
    tk = tkinter.Tk()
    tk.withdraw()
    try:
        for component, version, directory in (
            ("tcl", tk.tk.call("info", "patchlevel"), tk.tk.call("info", "library")),
            ("tk", tk.tk.call("package", "provide", "Tk"), tk.tk.eval("set tk_library")),
        ):
            version = str(version)
            path = Path(str(directory)) / "license.terms"
            target = notices / f"{component}-{version}-license.terms"
            url = f"https://raw.githubusercontent.com/tcltk/{component}/core-{version.replace('.', '-')}/license.terms"
            if path.is_file():
                shutil.copy2(path, target)
                acquired.append({"file": target.name, "source_url": url, "sha256": digest(target), "origin": "bundled Python runtime"})
            else:
                acquired.append(fetch(url, target))
    finally:
        tk.destroy()
    (notices / "NATIVE-LIBRARIES.txt").write_text(
        "This application uses a documented MIT-licensed GigaAM source patch that defers the optional\n"
        "soundfile import until the unused AudioDataset file-path helper is called. That helper is not\n"
        "provided by this desktop app. Its audio path uses standard-library PCM WAV and direct tensors.\n"
        "soundfile, libsndfile and libsndfile's statically linked codecs are NOT redistributed.\n"
        "Exact upstream/patched source, diff and checksums are in provenance/gigaam-lazy-soundfile-patch/.\n"
        "PyAudioWPatch 0.2.12.9 statically embeds its patched PortAudio in _portaudiowpatch*.pyd.\n"
        "It supports WASAPI/WMME/DirectX and is used here only for selected WASAPI inputs/loopbacks.\n"
        "No sounddevice, external PortAudio DLL, ASIO, virtual cable, meeting API, or Docker is included.\n"
        "Fork Apache-2.0 full terms, original PyAudio MIT notice, and exact fork PortAudio MIT terms\n"
        "are included in licenses/PyAudioWPatch. Native source/build details are at\n"
        f"https://github.com/s0d3s/PyAudioWPatch/tree/{PAWP_REVISION}.\n"
        "Tcl/Tk and Python PSF license texts are included separately.\n"
        "PyInstaller's license includes its bootloader exception. Torch/NumPy native third-party\n"
        "notices are preserved from their wheels. Microsoft VC runtime files are supplied with Python/\n"
        "PyTorch; the full Microsoft terms are in Microsoft-VC-Runtime-2015-2022-License.docx.\n"
        "See also https://learn.microsoft.com/en-us/visualstudio/releases/2022/redistribution .\n"
        "The application license does not replace third-party terms; this bundle is not entirely MIT-licensed.\n",
        encoding="utf-8")
    info = {"python": sys.version, "python_version": python_version,
            "python_source": f"https://www.python.org/ftp/python/{python_version}/Python-{python_version}.tgz",
            "python_distribution": "actual build interpreter, Windows x64; exact redistributed runtime files hashed in file-manifest.json",
            "python_license_sha256": digest(notices / "Python-LICENSE.txt"), "gigaam_code_revision": CODE_REVISION,
            "gigaam_source": f"https://github.com/salute-developers/GigaAM/tree/{CODE_REVISION}",
            "capture": {"package": "PyAudioWPatch", "version": "0.2.12.9", "source_revision": PAWP_REVISION,
                        "scope": "selected WASAPI device; no process-specific or default-device fallback",
                        "native_linkage": "PortAudio statically linked in _portaudiowpatch extension"},
            "packages": packages, "additional_artifacts": acquired,
            "inventory_scope": "Build environment and runtime dependencies; soundfile, ONNX, ONNXRuntime and ml-dtypes are installed only for upstream dependency validation and excluded from the frozen application, along with unused GigaAM ONNX/VAD helpers. Actual redistributed files are identified by file-manifest.json",
            "acquisition_reports": "provenance/*.json retain pip source URLs and archive SHA256 hashes; VCS package has immutable commit ID"}
    (bundle / "dependency-inventory.json").write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")
    fixture = args.reports.parent / "test-fixtures" / "example.wav"
    fetch(SAMPLE_URL, fixture, SAMPLE_SHA256, limit=1_000_000)


if __name__ == "__main__":
    main()
