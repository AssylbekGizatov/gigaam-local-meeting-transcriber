"""Opt-in separate PCM WAV files. Never used when save_audio=False."""
from datetime import datetime
import json
from pathlib import Path
import queue
import threading
import uuid
import wave

import numpy as np
from .storage import validate_local_path


class AudioRecorder:
    def __init__(self, folder: Path, on_error):
        folder = validate_local_path(folder)
        self.folder = folder / (datetime.now().strftime("audio-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8])
        self.folder.mkdir(parents=True, exist_ok=False)
        self.on_error = on_error
        self._queue = queue.Queue(32)
        self._files = {}
        self._entries = []
        self._failed = False
        self._thread = threading.Thread(target=self._run, name="optional-audio-writer", daemon=True)
        self._thread.start()

    def feed(self, block):
        if self._failed:
            return
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            self._fail()

    def _fail(self):
        if not self._failed:
            self._failed = True
            self.on_error("Не удалось сохранить аудио без потерь. Захват остановлен; проверьте свободное место и скорость диска.")

    def _run(self):
        while True:
            block = self._queue.get()
            if block is None:
                break
            if self._failed:
                continue
            try:
                self._write(block)
            except Exception:
                self._fail()
        for writer, entry in self._files.values():
            try:
                writer.close()
            except Exception:
                self._fail()
        try:
            with (self.folder / "timeline.json").open("x", encoding="utf-8") as out:
                json.dump({"schema": 1, "clock": "session monotonic seconds; pauses retained", "complete": not self._failed,
                           "files": self._entries}, out, ensure_ascii=False, indent=2)
        except Exception:
            self._fail()

    def _write(self, block):
        current = self._files.get(block.source)
        channels = block.samples.shape[1] if block.samples.ndim == 2 else 1
        if current:
            _, entry = current
            expected = entry["start"] + entry["frames"] / entry["sample_rate"]
            if (block.discontinuity or abs(block.start - expected) > 0.01 or entry["sample_rate"] != block.sample_rate
                    or entry["channels"] != channels or entry["frames"] >= 600 * block.sample_rate):
                current[0].close()
                current = None
        if current is None:
            name = f"{block.source}-{len(self._entries) + 1:05d}.wav"
            # Folder is exclusive and every filename is unique. WAV headers are
            # finalized on the next gap/resume/rotation/stop; no merged audio.
            writer = wave.open(str(self.folder / name), "wb")
            writer.setnchannels(channels)
            writer.setsampwidth(2)
            writer.setframerate(block.sample_rate)
            entry = {"file": name, "source": block.source, "start": block.start, "frames": 0,
                     "sample_rate": block.sample_rate, "channels": channels}
            self._entries.append(entry)
            current = writer, entry
            self._files[block.source] = current
        pcm = np.rint(np.clip(block.samples, -1, 1) * 32767).astype("<i2")
        current[0].writeframesraw(pcm.tobytes())
        current[1]["frames"] += len(pcm)

    def stop(self):
        self._queue.put(None)
        self._thread.join()
