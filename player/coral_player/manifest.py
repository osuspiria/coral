"""Manifest parsing, validation and the acceptance rules of spec §4.4.

The player trusts nothing it receives: structure, types and the playlist hash are
all checked before a manifest can be used.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SUPPORTED_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXTENSIONS = {"jpg", "png", "webp", "mp4", "mov", "mkv"}


class InvalidManifest(Exception):
    pass


class EmptyManifest(InvalidManifest):
    """Empty item list without an explicit cleared flag. Refused, never deletes anything."""


class Action(Enum):
    APPLY = "apply"  # download what is missing, then promote and play
    CLEAR = "clear"  # explicit clear: promote and show the fallback
    KEEP = "keep"  # no playlist assigned: keep whatever is playing


@dataclass(frozen=True)
class Item:
    sha256: str
    size: int
    url: str
    kind: str
    ext: str
    position: int
    start_date: date | None
    end_date: date | None
    duration: float | None

    @property
    def filename(self) -> str:
        return f"{self.sha256}.{self.ext}"


@dataclass(frozen=True)
class Manifest:
    assigned: bool
    cleared: bool
    timezone: str
    items: tuple[Item, ...]
    playlist_hash: str
    raw: dict[str, Any]

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def action(self) -> Action:
        if self.cleared:
            return Action.CLEAR
        if not self.assigned:
            return Action.KEEP
        return Action.APPLY


def canonical_hash(raw: dict[str, Any]) -> str:
    """Same canonical form as the dashboard (sorted keys, compact, UTF-8)."""
    body = {k: v for k, v in raw.items() if k != "playlist_hash"}
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(data.encode()).hexdigest()


def _date(value: Any, field: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidManifest(f"{field} must be a string or null")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise InvalidManifest(f"{field} is not a valid date: {value!r}") from exc


def _item(raw: Any, index: int) -> Item:
    if not isinstance(raw, dict):
        raise InvalidManifest(f"item {index} is not an object")
    sha = raw.get("sha256")
    if not isinstance(sha, str) or not _SHA256.match(sha):
        raise InvalidManifest(f"item {index}: invalid sha256")
    size = raw.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise InvalidManifest(f"item {index}: invalid size")
    url = raw.get("url")
    if not isinstance(url, str) or url != f"/api/v1/media/{sha}":
        raise InvalidManifest(f"item {index}: unexpected url")
    kind = raw.get("kind")
    if kind not in {"image", "video"}:
        raise InvalidManifest(f"item {index}: invalid kind")
    ext = raw.get("ext")
    if ext not in _EXTENSIONS:
        raise InvalidManifest(f"item {index}: invalid extension")
    position = raw.get("position")
    if not isinstance(position, int) or isinstance(position, bool):
        raise InvalidManifest(f"item {index}: invalid position")
    duration = raw.get("duration")
    if duration is not None and (
        not isinstance(duration, int | float) or isinstance(duration, bool) or duration <= 0
    ):
        raise InvalidManifest(f"item {index}: invalid duration")
    if kind == "image" and duration is None:
        raise InvalidManifest(f"item {index}: image without duration")
    start = _date(raw.get("start_date"), f"item {index} start_date")
    end = _date(raw.get("end_date"), f"item {index} end_date")
    if start and end and start > end:
        raise InvalidManifest(f"item {index}: start_date after end_date")
    return Item(sha, size, url, kind, ext, position, start, end, duration)


def parse_manifest(raw: Any) -> Manifest:
    """Validate a manifest. Raises InvalidManifest / EmptyManifest."""
    if not isinstance(raw, dict):
        raise InvalidManifest("manifest is not an object")
    if raw.get("version") != SUPPORTED_VERSION:
        raise InvalidManifest(f"unsupported manifest version {raw.get('version')!r}")
    assigned, cleared = raw.get("assigned"), raw.get("cleared")
    if not isinstance(assigned, bool) or not isinstance(cleared, bool):
        raise InvalidManifest("assigned/cleared must be booleans")
    timezone = raw.get("timezone")
    try:
        ZoneInfo(str(timezone))
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise InvalidManifest(f"unknown timezone {timezone!r}") from exc
    items_raw = raw.get("items")
    if not isinstance(items_raw, list):
        raise InvalidManifest("items must be a list")
    playlist_hash = raw.get("playlist_hash")
    if not isinstance(playlist_hash, str) or canonical_hash(raw) != playlist_hash:
        raise InvalidManifest("playlist_hash does not match the manifest content")

    items = tuple(sorted((_item(r, i) for i, r in enumerate(items_raw)), key=lambda i: i.position))
    if (cleared or not assigned) and items:
        raise InvalidManifest("a cleared or unassigned manifest must not carry items")
    if assigned and not cleared and not items:
        raise EmptyManifest("empty manifest without an explicit cleared flag")
    return Manifest(assigned, cleared, str(timezone), items, playlist_hash, raw)
