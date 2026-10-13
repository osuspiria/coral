"""Applies play lists to mpv without interrupting the item on screen (invariant 3).

The play list lives inside mpv (`--loop-playlist=inf`), so the screen keeps
cycling even if the agent dies. To apply a new list:

1. `playlist-clear` removes every entry except the one playing;
2. each new entry is appended with `loadfile <file> append` (named arguments,
   with the per-file `image-display-duration` option for images);
3. the old entry is remembered by its playlist entry id and removed with
   `playlist-remove` on the next `start-file` event, i.e. after it finished.

`loadlist`/`loadfile ... replace` are never used for content, because they
restart the current item. When the item on screen is the fallback image, the
controller skips to the new list right away (`playlist-next`): nothing to finish.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from .mpv_ipc import MpvError, MpvIpc
from .state import PlayEntry

log = logging.getLogger(__name__)


class MpvController:
    def __init__(
        self, ipc: MpvIpc, media_dir: Path, fallback: Path, fallback_duration_s: float
    ) -> None:
        self.ipc = ipc
        self.media_dir = media_dir
        self.fallback = fallback
        self.fallback_duration_s = fallback_duration_s
        self.applied: list[PlayEntry] | None = None
        self._stale_ids: set[int] = set()
        self._lock = asyncio.Lock()

    def reset(self) -> None:
        """Forget mpv's state (after a reconnect, mpv may have restarted)."""
        self.applied = None
        self._stale_ids.clear()

    def _targets(self, entries: list[PlayEntry]) -> list[tuple[str, dict[str, str]]]:
        if not entries:
            return [(str(self.fallback), self._image_opts(self.fallback_duration_s))]
        targets = []
        for entry in entries:
            path = str(self.media_dir / entry.filename)
            opts = self._image_opts(entry.duration) if entry.kind == "image" else {}
            targets.append((path, opts))
        return targets

    @staticmethod
    def _image_opts(duration: float | None) -> dict[str, str]:
        return {"image-display-duration": f"{duration:g}"} if duration else {}

    def _is_fallback(self, filename: str | None) -> bool:
        """mpv reports the path as it was given (e.g. through the `current` symlink in
        mpv.service), so compare real paths."""
        if not filename:
            return False
        return os.path.realpath(filename) == os.path.realpath(self.fallback)

    async def _loadfile(self, path: str, flags: str, opts: dict[str, str]) -> None:
        kwargs: dict[str, Any] = {"url": path, "flags": flags}
        if opts:
            kwargs["options"] = ",".join(f"{k}={v}" for k, v in opts.items())
        await self.ipc.command_named("loadfile", **kwargs)

    async def apply(self, entries: list[PlayEntry]) -> bool:
        """Make mpv play `entries` (fallback if empty). Returns True if anything changed."""
        async with self._lock:
            if self.applied is not None and entries == self.applied:
                return False
            targets = self._targets(entries)
            playlist = await self.ipc.get_property("playlist") or []
            current = next((e for e in playlist if e.get("current")), None)

            await self.ipc.command("playlist-clear")  # keeps the current entry, if any
            if current is None:
                for index, (path, opts) in enumerate(targets):
                    await self._loadfile(path, "append-play" if index == 0 else "append", opts)
                log.info("mpv was idle; started a list of %d entries", len(targets))
            else:
                for path, opts in targets:
                    await self._loadfile(path, "append", opts)
                self._stale_ids.add(int(current["id"]))
                if self._is_fallback(current.get("filename")) and entries:
                    # Nothing worth finishing on screen: leave the fallback right away.
                    await self.ipc.command("playlist-next", "force")
                    log.info("Replaced the fallback with a list of %d entries", len(targets))
                else:
                    log.info("Queued a list of %d entries after the current item", len(targets))
            self.applied = list(entries)
            return True

    async def remove_stale(self, playing_entry_id: int | None) -> None:
        """Called on start-file: drop entries of the previous list once they ended."""
        async with self._lock:
            stale = {i for i in self._stale_ids if i != playing_entry_id}
            if not stale:
                return
            try:
                playlist = await self.ipc.get_property("playlist") or []
            except MpvError as exc:
                log.warning("Could not read the mpv playlist: %s", exc)
                return
            # Remove from the end so earlier indexes stay valid.
            for index in sorted(
                (i for i, e in enumerate(playlist) if e.get("id") in stale), reverse=True
            ):
                try:
                    await self.ipc.command("playlist-remove", index)
                except MpvError as exc:
                    log.warning("Could not remove stale playlist entry %d: %s", index, exc)
            self._stale_ids -= stale

    async def status(self) -> dict[str, Any]:
        """What mpv is doing, for logs now and the heartbeat in phase 2."""
        try:
            path = await self.ipc.get_property("path")
        except MpvError:  # "property unavailable" while idle
            path = None
        return {"current_file": Path(path).name if path else None}
