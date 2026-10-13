"""Media cache: download to .staging, verify sha256 + size, promote atomically,
and clean up unused files after a grace period (spec §4.4)."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import time
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .client import ServerClient
from .config import Config
from .fsutil import fsync_dir
from .manifest import Item
from .state import State

log = logging.getLogger(__name__)

_MEDIA_NAME = re.compile(r"^[0-9a-f]{64}\.[a-z0-9]{2,5}$")


class SyncError(Exception):
    """A sync step failed; the previous playlist stays in place."""


class InsufficientSpace(SyncError):
    pass


class CorruptDownload(SyncError):
    pass


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


class MediaCache:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.media_dir = config.media_dir
        self.staging_dir = config.staging_dir

    def prepare(self) -> None:
        self.staging_dir.mkdir(parents=True, exist_ok=True)

    def path_for(self, filename: str) -> Path:
        return self.media_dir / filename

    def is_present(self, item: Item) -> bool:
        try:
            return self.path_for(item.filename).stat().st_size == item.size
        except FileNotFoundError:
            return False

    def missing(self, items: Iterable[Item]) -> list[Item]:
        unique: dict[str, Item] = {}
        for item in items:
            unique.setdefault(item.filename, item)
        return [i for i in unique.values() if not self.is_present(i)]

    def check_space(self, missing: list[Item]) -> None:
        needed = 0
        for item in missing:
            part = self.staging_dir / f"{item.sha256}.part"
            have = part.stat().st_size if part.exists() else 0
            needed += max(item.size - have, 0)
        free = shutil.disk_usage(self.media_dir).free
        reserve = self.config.min_free_mb * 1024 * 1024
        if free - needed < reserve:
            raise InsufficientSpace(
                f"not enough disk space: need {needed // 2**20} MB plus a reserve of "
                f"{self.config.min_free_mb} MB, {free // 2**20} MB free"
            )

    def fetch(self, item: Item, client: ServerClient) -> None:
        """Download, verify and promote one item. Never leaves a bad file in media/."""
        part = self.staging_dir / f"{item.sha256}.part"
        client.download(item.url, part, item.size)
        size = part.stat().st_size
        if size != item.size:
            part.unlink(missing_ok=True)
            raise CorruptDownload(f"{item.filename}: size {size}, expected {item.size}")
        actual = file_sha256(part)
        if actual != item.sha256:
            part.unlink(missing_ok=True)
            raise CorruptDownload(f"{item.filename}: sha256 mismatch ({actual[:12]}…)")
        os.replace(part, self.path_for(item.filename))
        fsync_dir(self.media_dir)
        log.info("Downloaded and verified %s (%d bytes)", item.filename, item.size)

    def ensure(self, items: Iterable[Item], client: ServerClient) -> int:
        """Make every item present and verified. Raises SyncError on any failure."""
        missing = self.missing(items)
        if not missing:
            return 0
        self.check_space(missing)
        for item in missing:
            # A wrong-size file in media/ is corrupt; it is replaced, not trusted.
            self.path_for(item.filename).unlink(missing_ok=True)
            self.fetch(item, client)
        return len(missing)

    def cleanup(self, referenced: set[str], state: State, now: datetime | None = None) -> list[str]:
        """Delete unreferenced media after the grace period, at most N per run.

        `referenced` must include both the promoted manifest and what is on screen.
        Unused partial downloads are removed after the same grace period.
        """
        now = now or datetime.now(UTC)
        grace = timedelta(days=self.config.cleanup_grace_days)
        removed: list[str] = []
        present = {
            p.name for p in self.media_dir.iterdir() if p.is_file() and _MEDIA_NAME.match(p.name)
        }
        for name in list(state.orphaned_since):
            if name not in present or name in referenced:
                del state.orphaned_since[name]
        for name in sorted(present - referenced):
            since = state.orphaned_since.setdefault(name, now.isoformat())
            if len(removed) >= self.config.max_deletions_per_run:
                continue
            if now - datetime.fromisoformat(since) >= grace:
                self.path_for(name).unlink(missing_ok=True)
                del state.orphaned_since[name]
                removed.append(name)
                log.info("Removed unused media %s", name)
        referenced_shas = {n.split(".", 1)[0] for n in referenced}
        cutoff = time.time() - grace.total_seconds()
        for part in self.staging_dir.glob("*.part"):
            if part.stem not in referenced_shas and part.stat().st_mtime < cutoff:
                part.unlink(missing_ok=True)
        return removed
