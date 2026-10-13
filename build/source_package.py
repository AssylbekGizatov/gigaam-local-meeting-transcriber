"""Publishable source allowlist, privacy audit and exclusive local packaging.

Build outputs, recordings, models, environments and acquisition logs are never
selected. Payload/ZIP creation requires an explicit finalized-docs flag; this
tool does not contact GitHub or any other service.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import unquote
import zipfile

ROOT_FILES = (
    "app.py", "requirements-runtime.txt", "requirements-build.txt", "GigaAM-Meeting.spec",
    "LICENSE", "NOTICE.md", "README.md", ".gitignore", "CHANGELOG.md",
)
BINARY_HASHES = {
    "fonts/DejaVuSans.ttf": "3fdf69cabf06049ea70a00b5919340e2ce1e6d02b0cc3c4b44fb6801bd1e0d22",
    "assets/asr-smoke.wav": "d8aaaa18a5098d7c6de0595ae7ac1e64cacd0d4022af3595213bdaf23be77e69",
}
RULES = {
    "github_credential": re.compile(r"(?:github_pat_[A-Za-z0-9_]{30,}|gh[pousr]_[A-Za-z0-9]{20,})"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    "windows_user_path": re.compile(r"(?i)\b[a-z]:(?:\\{1,4}|/)(?:users|documents and settings)(?:\\{1,4}|/)"),
    "unix_user_path": re.compile(r"/(?:home|Users)/[^\s/\\\"']+"),
    "meeting_url": re.compile(r"(?i)https?://(?:teams\.live\.com/meet/|teams\.microsoft\.com/l/meetup-join/|[a-z0-9.-]*zoom\.us/(?:j|s)/|meet\.google\.com/[a-z]{3}-[a-z]{4}-[a-z]{3})"),
    "signed_url": re.compile(r"(?i)(?:[?&]|&amp;)(?:x-amz-(?:signature|credential|security-token)|x-goog-(?:signature|credential)|signature|sig|access_token|token|authorization|auth)=[^&#\s\"']{8,}"),
    "bearer_credential": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{20,}"),
    "literal_secret_assignment": re.compile(r"(?im)^\s*[\"']?(?:[a-z0-9_]*_)?(?:api_key|access_token|client_secret|password|secret|token)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9._~+/=-]{20,}[\"']"),
}
MAX_TEXT_BYTES = 2_000_000
PUBLIC_TEXT_SUFFIXES = {".md", ".json", ".xml", ".txt", ".patch", ".py", ".ps1", ".yml", ".yaml"}


def source_paths(root: Path) -> list[Path]:
    """Only explicit paths and shallow source globs; never the entire tree."""
    selected = [root / name for name in ROOT_FILES]
    selected.extend(root / name for name in BINARY_HASHES)
    selected.append(root / "fonts/LICENSE.txt")
    for directory, patterns in (
        ("gigaam_meeting", ("*.py",)), ("tests", ("*.py",)),
        ("build", ("*.py", "*.ps1")), ("docs", ("*.md", "*.json")),
        (".github/workflows", ("*.yml", "*.yaml")),
    ):
        for pattern in patterns:
            selected.extend((root / directory).glob(pattern))
    verification = root / "docs/verification"
    if verification.is_dir():
        selected.extend(path for path in verification.rglob("*") if path.is_file() and path.suffix.lower() in {".md", ".json", ".xml"})
        patch_source = verification / "gigaam-lazy-soundfile-patch"
        selected.extend(patch_source.glob("*.py.txt"))
        selected.extend(patch_source.glob("*.patch"))
    return sorted(set(selected), key=lambda path: path.relative_to(root).as_posix())


def scan_text(relative: str, content: str) -> list[dict]:
    """Return location/rule only. Never print the credential or matched text."""
    findings = []
    # Percent-encoded URLs and JSON slash escapes must not hide a credential.
    views = [content, unquote(content).replace("\\/", "/")]
    seen = set()
    for view in views:
        for name, rule in RULES.items():
            for match in rule.finditer(view):
                line = view.count("\n", 0, match.start()) + 1
                identity = name, line
                if identity not in seen:
                    findings.append({"path": relative, "line": line, "rule": name})
                    seen.add(identity)
    return findings


def audit_source(root: Path) -> tuple[dict, list[dict]]:
    root = root.resolve()
    files, findings = [], []
    for path in source_paths(root):
        relative = path.relative_to(root).as_posix()
        if not path.is_file():
            findings.append({"path": relative, "rule": "required_file_missing"})
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            findings.append({"path": relative, "rule": "linked_file_outside_source"})
            continue
        raw = path.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        if relative in BINARY_HASHES:
            if checksum != BINARY_HASHES[relative]:
                findings.append({"path": relative, "rule": "public_binary_checksum_mismatch"})
            encoding, content = "base64", base64.b64encode(raw).decode("ascii")
        else:
            if len(raw) > MAX_TEXT_BYTES:
                findings.append({"path": relative, "rule": "unexpected_large_text_file"})
                continue
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                findings.append({"path": relative, "rule": "unexpected_non_utf8_source"})
                continue
            encoding = "utf-8"
            findings.extend(scan_text(relative, content))
        files.append({"path": relative, "mode": "100644", "type": "blob", "encoding": encoding,
                      "content": content, "bytes": len(raw), "sha256": checksum})
    report = {
        "schema_version": 1, "status": "failed" if findings else "passed",
        "method": "Explicit source allowlist, pinned public binary checksums and textual privacy patterns",
        "scope_limit": "Heuristic source review is not a guarantee against all encoded secrets or future manual changes; reviewers must inspect publication contents.",
        "file_count": len(files), "total_source_bytes": sum(item["bytes"] for item in files),
        "excluded": ["models", "dist", "environments", "recordings", "raw build logs", "acquisition reports", "generated build directories"],
        "findings": findings,
        "files": [{key: item[key] for key in ("path", "bytes", "sha256", "encoding")} for item in files],
    }
    return report, files


def write_json_exclusive(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def audit_public_trees(roots: list[Path]) -> dict:
    """Apply the same privacy scanner to final runtime docs/provenance.

    This scans only directories explicitly supplied by the build coordinator.
    Paths in the report are anonymous scope labels plus relative filenames;
    private absolute directory names and matching values are never retained.
    """
    files, findings = [], []
    for index, root in enumerate(roots, 1):
        scope = f"public_tree_{index}"
        root = root.resolve()
        if not root.is_dir():
            findings.append({"path": scope, "rule": "public_tree_missing"})
            continue
        selected = sorted(path for path in root.rglob("*") if path.is_file())
        if not selected:
            findings.append({"path": scope, "rule": "public_tree_empty"})
        for path in selected:
            relative = scope + "/" + path.relative_to(root).as_posix()
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                findings.append({"path": relative, "rule": "linked_public_file_outside_scope"})
                continue
            if path.suffix.lower() not in PUBLIC_TEXT_SUFFIXES:
                findings.append({"path": relative, "rule": "unexpected_public_artifact"})
                continue
            raw = path.read_bytes()
            if len(raw) > MAX_TEXT_BYTES:
                findings.append({"path": relative, "rule": "unexpected_large_public_report"})
                continue
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                findings.append({"path": relative, "rule": "unexpected_non_utf8_public_report"})
                continue
            findings.extend(scan_text(relative, content))
            files.append({"path": relative, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
    return {"status": "failed" if findings else "passed", "file_count": len(files), "files": files, "findings": findings}


def package_source(files: list[dict], destination: Path) -> tuple[Path, Path]:
    """ZIP exactly the audited in-memory bytes, not a later changing tree."""
    if destination.suffix.lower() != ".zip":
        raise ValueError("Source archive must have a .zip extension")
    checksum = destination.with_suffix(".zip.sha256")
    if destination.exists() or checksum.exists():
        raise FileExistsError("Source archive/checksum already exists; select new names")
    destination.parent.mkdir(parents=True, exist_ok=True)
    created = checksum_created = False
    try:
        with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zipped:
            created = True
            for item in files:
                data = item["content"].encode("utf-8") if item["encoding"] == "utf-8" else base64.b64decode(item["content"], validate=True)
                if hashlib.sha256(data).hexdigest() != item["sha256"]:
                    raise ValueError("Audited source payload checksum mismatch")
                info = zipfile.ZipInfo("gigaam-local-meeting-transcriber/" + item["path"])
                info.external_attr = 0o100644 << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                zipped.writestr(info, data)
        digest = hashlib.sha256(destination.read_bytes()).hexdigest()
        with checksum.open("x", encoding="ascii") as stream:
            checksum_created = True
            stream.write(f"{digest}  {destination.name}\n")
    except BaseException:
        # Only our newly exclusively created archive may be removed.
        if created:
            destination.unlink(missing_ok=True)
        if checksum_created:
            checksum.unlink(missing_ok=True)
        raise
    return destination, checksum


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--audit", type=Path, help="New local audit JSON; existing reports are preserved")
    parser.add_argument("--payload", type=Path, help="New JSON with UTF-8 source and base64 public binaries")
    parser.add_argument("--zip", dest="archive", type=Path, help="New source ZIP and SHA256 sidecar")
    parser.add_argument("--scan-public-tree", type=Path, action="append", default=[], help="Also audit an explicitly curated runtime docs/provenance directory; repeat for multiple scopes")
    parser.add_argument("--finalized-docs", action="store_true", help="Explicitly confirm final docs/evidence have been curated before snapshot creation")
    args = parser.parse_args()
    if (args.payload or args.archive) and not args.finalized_docs:
        parser.error("Payload/ZIP creation requires --finalized-docs after the final documentation review")
    report, files = audit_source(args.root)
    if args.scan_public_tree:
        extra = audit_public_trees(args.scan_public_tree)
        report["public_tree_audit"] = extra
        report["findings"].extend(extra["findings"])
        report["status"] = "failed" if report["findings"] else "passed"
    if args.audit:
        write_json_exclusive(args.audit, report)
    print(f"Source privacy audit: {report['status']}; {report['file_count']} allowlisted files, {report['total_source_bytes']} bytes")
    for item in report["findings"]:
        print(f"{item['path']}:{item.get('line', 0)}: {item['rule']}")
    if report["status"] != "passed":
        return 2
    if args.payload:
        write_json_exclusive(args.payload, {"schema_version": 1, "audit": report, "files": files})
    if args.archive:
        package_source(files, args.archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
