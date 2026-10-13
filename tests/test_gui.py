"""Withdrawn Tk checks in isolated processes; no audio or native UI control.

Isolation keeps a destroyed Tk interpreter's object cycles away from later
unrelated ASR test workers. The actual application has one Tk per process.
"""
import gc
from pathlib import Path
from queue import Queue
import subprocess
import sys
from threading import Event as Signal, enumerate as threads
from time import monotonic, sleep
import tkinter as tk
import weakref

import pytest


class FakeManager:
    def __init__(self):
        self.entered = Signal()
        self.release = Signal()
        self.fail = False
        self.closed = False
        self.starts = []
        self.previews = []
        self.stop_calls = 0

    def devices(self):
        from gigaam_meeting.contracts import AudioDevice
        self.entered.set()
        assert self.release.wait(6)
        if self.fail:
            raise RuntimeError("private diagnostic content must not leak")
        return {
            "microphone": [AudioDevice(1, "Fake microphone", "microphone", 16000, 1, fingerprint="mic")],
            "system": [AudioDevice(2, "Fake headphones", "system", 48000, 2, fingerprint="system")],
        }

    def start(self, config):
        self.starts.append(config)

    def preview(self, config, seconds):
        self.previews.append((config, seconds))

    def stop(self):
        self.stop_calls += 1

    def close(self):
        self.closed = True


def catalog_ready(app, manager, events):
    manager.release.set()
    deadline = monotonic() + 2
    while events.empty():
        if monotonic() > deadline:
            raise AssertionError("Fake catalog result was not delivered")
        sleep(0.005)
    app._poll()
    assert not app.enumerating


CASES = ["catalog", "consent", "preview", "stopping", "safe_error", "close", "worker_lifetime", "echo"]


@pytest.mark.parametrize("case", CASES)
def test_hidden_gui_lifecycle(case):
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--child", case],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        timeout=15,
    )
    if result.returncode == 77:
        pytest.skip("Tk display unavailable; this test never needs audio hardware")
    assert result.returncode == 0, result.stdout + result.stderr


def run_case(case):
    from gigaam_meeting.contracts import Event
    import gigaam_meeting.gui as gui_module
    from gigaam_meeting.gui import MeetingApp
    try:
        root = tk.Tk()
    except tk.TclError:
        return 77
    root.withdraw()
    manager, events = FakeManager(), Queue()
    app = None
    try:
        before = monotonic()
        app = MeetingApp(root, manager, events)
        duration = monotonic() - before
        assert manager.entered.wait(1)
        if case == "catalog":
            assert duration < 1, "Catalog driver work must run outside the Tk callback"
            assert app.enumerating
            for button in (app.start_button, app.preview_button, app.refresh_button):
                assert str(button["state"]) == "disabled"
            assert str(app.combos["microphone"]["state"]) == "disabled"
            assert not manager.starts and not manager.previews
            catalog_ready(app, manager, events)
            assert app.combos["microphone"].current() == app.combos["system"].current() == 0
            assert not app.consent.get() and not app.save_audio.get()
            assert not manager.starts and not manager.previews
        elif case == "consent":
            catalog_ready(app, manager, events)
            app.combos["microphone"].current(1)
            warnings = []
            gui_module.messagebox.showwarning = lambda *args, **kwargs: warnings.append(args)
            app._start()
            app._preview()
            assert len(warnings) == 2
            assert not manager.starts and not manager.previews
            assert app.state == "idle" and not app.consent.get()
        elif case == "preview":
            catalog_ready(app, manager, events)
            app.combos["microphone"].current(1)
            app.consent.set(True)
            app.save_audio.set(True)
            app._preview()
            config, seconds = manager.previews[0]
            assert config.consent and not config.save_audio and seconds == 3
            assert app.state == "preview" and not manager.starts
            events.put(Event("finished"))
            app._poll()
            assert app.state == "idle" and not app.consent.get() and not app.save_audio.get()
            assert not manager.starts
        elif case == "stopping":
            catalog_ready(app, manager, events)
            events.put(Event("state", data={"state": "starting"}))
            app._poll()
            assert app.state == "starting" and app.indicator.cget("bg") == "#b5212c"
            assert "захват может начаться" in app.status.get()
            assert str(app.quick_stop_button["state"]) == "normal"
            for button in (app.start_button, app.pause_button, app.export_button):
                assert str(button["state"]) == "disabled"
            app._set_state("recording")
            app._stop()
            assert manager.stop_calls == 1 and app.state == "stopping"
            assert app.indicator.cget("bg") == "#b5212c"
            assert str(app.quick_stop_button["state"]) == str(app.start_button["state"]) == "disabled"
            assert "дождитесь" in app.status.get()
            events.put(Event("state", data={"state": "draining"}))
            app._poll()
            assert app.state == "draining" and app.indicator.cget("bg") != "#b5212c"
            events.put(Event("finished"))
            app._poll()
            assert app.state == "idle" and not app.consent.get()
        elif case == "safe_error":
            manager.fail = True
            catalog_ready(app, manager, events)
            assert app.state == "error"
            assert "private diagnostic" not in app.alert_text.get()
            assert not manager.starts and not manager.previews
            assert not app.consent.get()
            assert str(app.refresh_button["state"]) == "normal"
        elif case == "close":
            destroyed = []
            destroy_root = root.destroy
            root.destroy = lambda: destroyed.append(True)
            try:
                before = monotonic()
                app._close_requested()
                assert monotonic() - before < 0.2
                assert manager.closed and destroyed
                assert not manager.starts and not manager.previews
            finally:
                root.destroy = destroy_root
        elif case == "echo":
            from gigaam_meeting.contracts import Segment
            catalog_ready(app, manager, events)
            before = Segment("microphone", 0, .5, "before")
            duplicated = Segment("microphone", 1, 5, "echo and local reply")
            after = Segment("microphone", 8, 9, "after")
            reference = Segment("system", 1.1, 3, "echo")
            local = Segment("microphone", 4, 5, "local reply")
            for segment in (before, duplicated, after):
                events.put(Event("segment", data={"segment": segment}))
            events.put(Event("echo_update", data={"removed": [duplicated], "added": [reference, local]}))
            events.put(Event("echo_stats", data={"hidden_words": 3}))
            app._poll()
            assert app.segments == [before, reference, local, after]
            text = app.transcript.get("1.0", "end")
            assert "echo and local reply" not in text
            assert text.index("before") < text.index("echo") < text.index("local reply") < text.index("after")
            assert len(app.segment_marks) == 4 and app.dirty
            assert app.echo_status.get().endswith("3 слов")
            # Removal of the last segment must not leave a stale header or mark.
            app._echo_update([after], [])
            assert "after" not in app.transcript.get("1.0", "end") and len(app.segment_marks) == 3
            app.combos["microphone"].current(1)
            app.combos["system"].current(1)
            app.consent.set(True)
            app.reduce_echo.set(False)
            app.dirty = False
            app._start()
            assert not manager.starts[-1].reduce_echo
            assert str(app.echo_check["state"]) == "disabled"
            events.put(Event("finished"))
            app._poll()
            assert str(app.echo_check["state"]) == "normal"
        elif case == "worker_lifetime":
            # A catalog worker may outlive window close, but must not retain Tk.
            reference = weakref.ref(app)
            app._close_requested()
            app = None
            gc.collect()
            assert reference() is None, "A background catalog worker retained the destroyed GUI"
            assert not manager.release.is_set() and manager.closed
        else:
            raise AssertionError("Unknown isolated GUI scenario")
        return 0
    finally:
        manager.release.set()
        for thread in threads():
            if thread.name == "wasapi-device-catalog":
                thread.join(timeout=1)
        manager.close()
        enabled = gc.isenabled()
        gc.disable()
        try:
            for identifier in root.tk.call("after", "info"):
                try:
                    root.after_cancel(identifier)
                except tk.TclError:
                    pass
            try:
                root.destroy()
            except tk.TclError:
                pass
            app = None
            root = None
            gc.collect()
        finally:
            if enabled:
                gc.enable()


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(run_case(sys.argv[2]))
