"""Filesystem helpers for a root process that reads files written by an
unprivileged one: never follow symlinks, bound sizes, write atomically."""

from __future__ import annotations

import contextlib
import json
import os
import stat
from pathlib import Path
from typing import Any

MAX_INPUT_BYTES = 4096


def read_untrusted_json(path: Path, max_bytes: int = MAX_INPUT_BYTES) -> Any | None:
    """Read a small JSON file written by the agent. Symlinks, non-regular files,
    oversized or invalid content all read as None."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
            return None
        data = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def read_json(path: Path) -> Any | None:
    """Read the updater's own state (root-owned directory)."""
    try:
        return json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path: Path, data: Any, mode: int = 0o644) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_dir(path.parent)


def switch_symlink(link: Path, target: Path) -> None:
    """Point `link` at `target` atomically: new symlink + rename over the old one."""
    tmp = link.with_name(f".{link.name}.new")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, link)
    _fsync_dir(link.parent)


def link_target(link: Path) -> Path | None:
    try:
        return Path(os.readlink(link))
    except OSError:
        return None
