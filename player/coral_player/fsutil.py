"""Crash-safe file writes: temp file in the same directory, fsync, rename, fsync dir."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def fsync_dir(path: Path) -> None:
    """Persist a rename. Not possible (nor needed) on Windows."""
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
    fsync_dir(path.parent)


def atomic_write_json(path: Path, data: Any) -> None:
    atomic_write_bytes(path, json.dumps(data, indent=2, sort_keys=True).encode())


def read_json(path: Path) -> Any | None:
    """Read JSON, returning None if the file is missing or unreadable."""
    try:
        return json.loads(path.read_bytes())
    except (FileNotFoundError, ValueError):
        return None
