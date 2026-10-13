"""Restrict app writes to explicitly selected absolute local filesystem paths."""
from __future__ import annotations

import ctypes
import os
from pathlib import Path


def _windows_drive_type(anchor: str) -> int:
    function = ctypes.windll.kernel32.GetDriveTypeW
    function.argtypes = [ctypes.c_wchar_p]
    function.restype = ctypes.c_uint
    return int(function(anchor))


def _reject_remote(path: Path) -> None:
    text = str(path)
    if text.startswith(("\\\\", "//")):
        raise ValueError("Выберите локальную папку. Сетевые пути UNC не поддерживаются.")
    if os.name == "nt":
        kind = _windows_drive_type(path.anchor)
        if kind == 4:
            raise ValueError("Выберите локальный диск. Подключённые сетевые диски не поддерживаются.")
        if kind in {0, 1}:
            raise ValueError("Локальный диск недоступен. Выберите другую папку.")


def validate_local_path(path: Path | str) -> Path:
    """Validate without creating folders/files; reject UNC before metadata access.

    This also checks the resolved target for ordinary links/junctions. It cannot
    detect sync agents, backups, or every filesystem/filter-driver remapping;
    those are controlled by Windows and the user, outside this application's
    offline policy.
    """
    destination = Path(path).expanduser()
    _reject_remote(destination)
    if not destination.is_absolute():
        raise ValueError("Выберите полный путь к локальной папке или файлу.")
    try:
        resolved = destination.resolve(strict=False)
    except (OSError, RuntimeError):
        raise ValueError("Не удалось проверить локальную папку. Выберите другой путь.") from None
    _reject_remote(resolved)
    return resolved
