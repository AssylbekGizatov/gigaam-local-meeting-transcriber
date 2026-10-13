"""Curate explicitly selected current reports; never copy a raw build tree."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import urllib.parse
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PRIVATE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]|/(?:Users|home)/|file://", re.I)
SIGNED = re.compile(r"[?&](?:Expires|Signature|Key-Pair-Id|token|access_token)=", re.I)
OMIT_KEYS = {"hostname", "computername", "computer_name", "username", "userprofile", "cwd", "executable", "home", "base_prefix"}
SOURCES = {"fixture_source": "https://cdn.chatwm.opensmodel.sberdevices.ru/GigaAM/example.wav",
           "english_source": "https://huggingface.co/datasets/hf-internal-testing/librispeech_asr_dummy/tree/5be91486e11a2d616f4ec5db8d3fd248585ac07a"}
LIMITS = {
    "platform": "Windows x64, bundled CPU runtime; no separate Python/Docker/FFmpeg required",
    "capture": "Selected WASAPI devices only; whole output device includes other applications; process-specific capture unsupported",
    "selection": "No automatic default-device fallback; fingerprints use PortAudio metadata, which has no stable endpoint GUID",
    "hardware": "Physical microphone/loopback acceptance remains a separate manual check; fake-driver and public-PCM tests do not establish hardware behavior",
    "languages": "Small public Russian/English read-speech fixtures and synthetic concatenation are limited examples; Kazakh and natural mixed speech remain unverified",
    "network": "Python guard plus sampled process TCP/UDP endpoints are limited evidence; no clean VM or physical network disconnection certification",
    "native": "PE imports/delay imports are audited; arbitrary dynamic LoadLibrary paths are not fully proven",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def public(value):
    if isinstance(value, dict):
        return {key: SOURCES.get(key, public(child)) for key, child in value.items() if key.lower() not in OMIT_KEYS}
    if isinstance(value, list):
        return [public(child) for child in value]
    if isinstance(value, str):
        if PRIVATE_PATH.search(value):
            return "local build path omitted"
        if SIGNED.search(value):
            if value.startswith(("http://", "https://")):
                url = urllib.parse.urlsplit(value)
                return urllib.parse.urlunsplit((url.scheme, url.netloc, url.path, "", ""))
            return "signed transport URL omitted"
    return value


def json_bytes(value):
    data = json.dumps(public(value), ensure_ascii=False, indent=2).encode("utf-8")
    if PRIVATE_PATH.search(data.decode()) or SIGNED.search(data.decode()):
        raise RuntimeError("Public report still contains private paths or signed transport data")
    return data


def load(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def verify_sources(report):
    expected = {"app.py", *(path.relative_to(ROOT).as_posix() for path in (ROOT / "gigaam_meeting").glob("*.py"))}
    actual = {item["file"] for item in report.get("files", [])}
    if report.get("status") != "passed" or actual != expected:
        raise RuntimeError("Final frozen-source attestation is incomplete")
    for item in report["files"]:
        if not item.get("bytecode_matches") or digest((ROOT / item["file"]).read_bytes()) != item["source_sha256"]:
            raise RuntimeError("Final source attestation is stale; rebuild/reverify before publication")
    if "exception tables" not in report.get("method", ""):
        raise RuntimeError("Use the strengthened frozen-source verifier for final publication")


def curate_junit(path):
    tree = ET.parse(path)
    for suite in tree.getroot().iter("testsuite"):
        if int(suite.get("failures", "0")) or int(suite.get("errors", "0")):
            raise RuntimeError("Final JUnit contains failures/errors")
    for element in tree.getroot().iter():
        for key, value in list(element.attrib.items()):
            if key.lower() in OMIT_KEYS or PRIVATE_PATH.search(value):
                del element.attrib[key]
        for child in list(element):
            if child.tag in {"properties", "system-out", "system-err"}:
                element.remove(child)
    data = ET.tostring(tree.getroot(), encoding="utf-8", xml_declaration=True)
    if PRIVATE_PATH.search(data.decode()) or SIGNED.search(data.decode()):
        raise RuntimeError("Final JUnit contains uncurated private data")
    return data


def acquisition(report):
    packages = []
    for item in report.get("install", []):
        info = item.get("download_info", {})
        url = info.get("url", "")
        if not url.startswith(("https://", "git+https://")) or PRIVATE_PATH.search(url) or SIGNED.search(url):
            continue  # Local wheel paths are not public acquisition evidence.
        packages.append({"name": item.get("metadata", {}).get("name"), "version": item.get("metadata", {}).get("version"),
                         "url": url, "archive_info": info.get("archive_info"), "vcs_info": info.get("vcs_info")})
    return {"scope": "Only public immutable/source URLs and artifact hashes; raw pip metadata/environment omitted", "packages": packages}


def validate_payloads(payloads):
    """Use the source-publication privacy rules before writing any evidence."""
    spec = importlib.util.spec_from_file_location("evidence_publication_scanner", ROOT / "build/source_package.py")
    scanner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scanner)
    findings = []
    for name, data in payloads.items():
        findings.extend(scanner.scan_text(name, data.decode("utf-8")))
    if findings:
        locations = "; ".join(f"{item['path']}:{item['line']}: {item['rule']}" for item in findings)
        raise RuntimeError("Public evidence privacy audit failed: " + locations)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--junit", type=Path)
    parser.add_argument("--streaming", type=Path)
    parser.add_argument("--language", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--inventory", type=Path)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--model", type=Path, default=ROOT / "models/gigaam/provenance.json")
    args = parser.parse_args()
    inputs, output = args.input.resolve(), args.output.resolve()
    attestation = load(inputs / "frozen-source-match.json")
    verify_sources(attestation)
    payloads = {"frozen-source-match.json": json_bytes(attestation)}
    for name in ("frozen-selftest.json", "runtime-network-audit.json"):
        report = load(inputs / name)
        if report.get("status", "passed") != "passed" or report.get("exit_code", 0) != 0:
            raise RuntimeError("Final native verification did not pass")
        payloads[name] = json_bytes(report)
    junit = args.junit or inputs / "unit-tests.xml"
    payloads["unit-tests.xml"] = curate_junit(junit)
    if args.baseline:
        payloads["source-baseline.json"] = json_bytes({"scope": "Earlier source baseline only; final frozen attestation is reported separately",
                                                       "report": load(args.baseline)})
    for name, selected in (("real-streaming-pcm.json", args.streaming), ("public-language-evaluation.json", args.language)):
        path = selected or inputs / name
        if path.is_file():
            report = load(path)
            if report.get("status", "passed") != "passed":
                raise RuntimeError("Selected source verification did not pass")
            for source, sha in report.get("reviewed_source_sha256", {}).items():
                path = ROOT / source if "/" in source or source == "app.py" else ROOT / "gigaam_meeting" / source
                if digest(path.read_bytes()) != sha:
                    raise RuntimeError("Selected public-PCM report is stale")
            payloads[name] = json_bytes(report)
    if args.provenance:
        patch = args.provenance / "gigaam-lazy-soundfile-patch"
        evidence = load(patch / "provenance.json")
        for name, key in (("utils.upstream.py.txt", "original_sha256_lf"), ("utils.patched.py.txt", "patched_sha256_lf")):
            text = (patch / name).read_text(encoding="utf-8")
            if digest(text.encode()) != evidence[key]:
                raise RuntimeError("GigaAM source patch evidence hash mismatch")
            payloads["gigaam-lazy-soundfile-patch/" + name] = text.encode()
        payloads["gigaam-lazy-soundfile-patch/change.patch"] = (patch / "change.patch").read_bytes()
        payloads["gigaam-lazy-soundfile-patch/provenance.json"] = json_bytes(evidence)
        for name in ("gigaam-acquisition.json", "torch-acquisition.json"):
            path = args.provenance / name
            if path.is_file():
                payloads[name] = json_bytes(acquisition(load(path)))
    if args.inventory:
        inventory = load(args.inventory)
        for package in inventory["packages"]:
            for notice in package["notices"]:
                name = notice["file"].replace("\\", "/")
                if "/" not in name:
                    name = "licenses/" + package["name"] + "/" + name
                path = args.inventory.parent / name
                if not path.is_file() or digest(path.read_bytes()) != notice["sha256"]:
                    raise RuntimeError("License inventory file/hash mismatch")
                notice["file"] = name
        inventory["acquisition_reports"] = "Curated provenance preserves available public source URLs, immutable revisions and artifact hashes; raw pip environment metadata and local wheel-cache paths are omitted"
        payloads["dependency-inventory.json"] = json_bytes(inventory)
    if args.model.is_file():
        model = load(args.model)
        verified = load(inputs / "frozen-selftest.json").get("model", {}).get("sha256", {})
        for name in ("config.json", "pytorch_model.bin"):
            if model.get("sha256", {}).get(name) != verified.get(name):
                raise RuntimeError("Model provenance does not match the final frozen-model hashes")
        model["artifact_sources"] = {name: (
            "https://raw.githubusercontent.com/salute-developers/GigaAM/" + model["code_revision"] + "/LICENSE"
            if name == "LICENSE" else
            "https://huggingface.co/" + model["repository"] + "/resolve/" + model["revision"] + "/" + name)
            for name in model["sha256"]}
        payloads["model-acquisition.json"] = json_bytes(model)
    payloads["deployment-limits.json"] = json_bytes(LIMITS)
    payloads["evidence-index.json"] = json_bytes({"scope": "Explicit current report whitelist; no raw build-tree merge",
        "verifier_sha256": digest((ROOT / "build/verify_source.py").read_bytes()),
        "files": [{"file": name, "sha256": digest(data)} for name, data in sorted(payloads.items())]})
    validate_payloads(payloads)
    if output.exists() and any(output.iterdir()):
        raise RuntimeError("Output folder is not empty; select a fresh evidence destination")
    output.mkdir(parents=True, exist_ok=True)
    for name, data in payloads.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(data)
    print("Curated", len(payloads), "public evidence files")


if __name__ == "__main__":
    main()
