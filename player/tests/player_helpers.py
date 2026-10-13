from __future__ import annotations

import hashlib
from typing import Any

from coral_player.manifest import canonical_hash


def blob(n: int, size: int = 2048) -> bytes:
    """Deterministic file content number n."""
    seed = hashlib.sha256(str(n).encode()).digest()
    return (seed * (size // len(seed) + 1))[:size]


def item_for(
    content: bytes,
    position: int,
    kind: str = "image",
    ext: str = "png",
    start: str | None = None,
    end: str | None = None,
    duration: float | None = 10,
) -> dict[str, Any]:
    sha = hashlib.sha256(content).hexdigest()
    return {
        "sha256": sha,
        "size": len(content),
        "url": f"/api/v1/media/{sha}",
        "kind": kind,
        "ext": ext,
        "position": position,
        "start_date": start,
        "end_date": end,
        "duration": duration,
    }


def make_manifest(
    items: list[dict[str, Any]],
    *,
    assigned: bool = True,
    cleared: bool = False,
    timezone: str = "Europe/Lisbon",
) -> dict[str, Any]:
    raw = {
        "version": 1,
        "assigned": assigned,
        "cleared": cleared,
        "timezone": timezone,
        "items": items,
    }
    raw["playlist_hash"] = canonical_hash(raw)
    return raw
