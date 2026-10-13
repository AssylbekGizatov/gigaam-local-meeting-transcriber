"""Compare frozen application bytecode against reviewed source (ignoring paths)."""
import argparse
import hashlib
import json
import marshal
from pathlib import Path
import types

from PyInstaller.archive.readers import CArchiveReader


def canonical(value):
    if isinstance(value, types.CodeType):
        return {"code": value.co_code.hex(), "constants": canonical(value.co_consts),
                "exception_table": value.co_exceptiontable.hex(), "stacksize": value.co_stacksize,
                "name": value.co_name, "qualname": value.co_qualname,
                "names": value.co_names, "varnames": value.co_varnames, "freevars": value.co_freevars,
                "cellvars": value.co_cellvars, "args": (value.co_argcount, value.co_posonlyargcount, value.co_kwonlyargcount),
                "flags": value.co_flags}
    if isinstance(value, (tuple, list)):
        return [canonical(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(repr(item) for item in value)
    if isinstance(value, bytes):
        return {"bytes": value.hex()}
    return repr(value)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--exe", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    archive = CArchiveReader(str(args.exe))
    pyz_name = next(name for name in archive.toc if name.endswith(".pyz"))
    pyz = archive.open_embedded_archive(pyz_name)
    checks = []
    for source in (root / "app.py", *sorted((root / "gigaam_meeting").glob("*.py"))):
        relative = source.relative_to(root).as_posix()
        name = relative.removesuffix(".py").replace("/", ".").removesuffix(".__init__")
        actual = marshal.loads(archive.extract("app")) if name == "app" else pyz.extract(name)
        expected = compile(source.read_bytes(), str(source), "exec", dont_inherit=True, optimize=0)
        if canonical(actual) != canonical(expected):
            raise RuntimeError(f"Frozen module differs from reviewed source: {relative}")
        checks.append({"file": relative, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(), "bytecode_matches": True})
    with args.report.open("x", encoding="utf-8") as out:
        json.dump({"status": "passed", "method": "Python bytecode/constants, exception tables, stack sizes and names compared; debug paths/line tables excluded", "files": checks}, out, indent=2)


if __name__ == "__main__":
    main()
