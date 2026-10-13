from __future__ import annotations

from coral_dashboard.manifest import ManifestContext
from coral_dashboard.repo import Store

CTX = ManifestContext(timezone="Europe/Lisbon", default_image_duration_s=10)


def add_media(store: Store, n: int = 1, kind: str = "image", size: int = 100) -> int:
    sha = f"{n:064x}"
    ext = "png" if kind == "image" else "mp4"
    return store.add_media(
        sha256=sha,
        stored_name=f"{sha}.{ext}",
        original_name=f"file{n}.{ext}",
        kind=kind,
        size=size,
        duration_s=None if kind == "image" else 12.5,
        codec="png" if kind == "image" else "h264",
        width=1920,
        height=1080,
    )
