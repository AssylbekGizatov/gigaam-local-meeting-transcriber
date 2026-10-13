"""Portable desktop entry point. No hardware capture until an explicit UI action."""
import argparse
from pathlib import Path
import sys

from gigaam_meeting.offline import install
install()


def main():
    from gigaam_meeting.storage import validate_local_path
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", type=Path, metavar="NEW_REPORT.json")
    parser.add_argument("--list-devices", type=Path, metavar="NEW_REPORT.json")
    args = parser.parse_args()
    if args.self_test:
        from gigaam_meeting.engine import bundle_root
        from gigaam_meeting.selftest import run_self_test
        result = run_self_test(bundle_root(), validate_local_path(args.self_test.absolute()))
        return 0 if result["status"] == "passed" else 1
    if args.list_devices:
        import dataclasses
        import json
        from gigaam_meeting.audio import list_devices
        with validate_local_path(args.list_devices.absolute()).open("x", encoding="utf-8") as stream:
            json.dump({k: [dataclasses.asdict(d) for d in values] for k, values in list_devices().items()},
                      stream, ensure_ascii=False, indent=2)
        return 0
    from gigaam_meeting.gui import run_app
    from gigaam_meeting.session import SessionManager
    run_app(SessionManager)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # No traceback, speech, userpaths, or crash upload. Errors in active
        # sessions are handled by the coordinator before arriving here.
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("GigaAM", "Не удалось открыть приложение. Полностью распакуйте ZIP в доступную локальную папку.")
        root.destroy()
        sys.exit(1)
