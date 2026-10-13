"""Player manifest construction and playlist hash (spec §3.3, §4.4)."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

MANIFEST_VERSION = 1


@dataclass(frozen=True)
class ManifestContext:
    timezone: str
    default_image_duration_s: int


def canonical_json(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def manifest_hash(manifest: Mapping[str, Any]) -> str:
    """sha256 of the canonical manifest, excluding the hash field itself."""
    body = {k: v for k, v in manifest.items() if k != "playlist_hash"}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def manifest_item(row: Mapping[str, Any], ctx: ManifestContext) -> dict[str, Any]:
    """Build one manifest item from a joined playlist_items + media row."""
    if row["kind"] == "image":
        duration = row["image_duration_s"] or ctx.default_image_duration_s
    else:
        duration = row["duration_s"]
    stored_name: str = row["stored_name"]
    return {
        "sha256": row["sha256"],
        "size": row["size"],
        "url": f"/api/v1/media/{row['sha256']}",
        "kind": row["kind"],
        "ext": stored_name.rsplit(".", 1)[-1],
        "position": row["position"],
        "start_date": row["start_date"],
        "end_date": row["end_date"],
        "duration": duration,
    }


def build_manifest(
    rows: Iterable[Mapping[str, Any]],
    ctx: ManifestContext,
    *,
    assigned: bool,
    cleared: bool,
) -> dict[str, Any]:
    """Build a manifest. A cleared or unassigned manifest never carries items."""
    items = [] if (cleared or not assigned) else [manifest_item(r, ctx) for r in rows]
    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "assigned": assigned,
        "cleared": cleared,
        "timezone": ctx.timezone,
        "items": sorted(items, key=lambda i: i["position"]),
    }
    manifest["playlist_hash"] = manifest_hash(manifest)
    return manifest
