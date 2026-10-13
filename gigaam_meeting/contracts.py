from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

Source = Literal["microphone", "system"]
SOURCE_LABELS = {"microphone": "Микрофон", "system": "Звук компьютера"}


@dataclass(frozen=True)
class AudioDevice:
    id: int
    name: str
    source: Source
    sample_rate: int
    channels: int
    host_api: str = "WASAPI"
    is_default: bool = False
    fingerprint: str = ""


@dataclass(frozen=True)
class AudioBlock:
    source: Source
    samples: Any  # NumPy float32 [frames, channels], owned copy, normalized [-1,1]
    sample_rate: int
    start: float  # seconds on one session monotonic clock; includes pause gaps
    discontinuity: bool = False


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Segment:
    source: Source
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class SessionConfig:
    microphone: AudioDevice | None
    system: AudioDevice | None
    consent: bool
    output_folder: Path
    save_audio: bool = False


@dataclass(frozen=True)
class Event:
    kind: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
