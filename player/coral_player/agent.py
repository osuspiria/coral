"""signage-agent: sync, playlist engine and mpv control in one asyncio process.

Flow (spec §2.3, §4.4, §4.5):
- a ChangeSource says when to fetch the manifest (polling now, heartbeat in phase 2);
- a valid manifest is acted on by its action: APPLY downloads what is missing and
  promotes it only when every file is verified; CLEAR promotes it and shows the
  fallback; KEEP (no playlist assigned) changes nothing;
- the engine filters the promoted manifest by date, every 5 min and after local
  midnight, and pushes the result to mpv without interrupting the current item
  (the one exception is an explicit clear, which cuts to the fallback at once);
- with an invalid clock, the last list sent to mpv stays.

Every failure keeps the screen as it is and is logged and recorded in state.json.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .changes import ChangeSource, SyncTrigger
from .client import ServerClient, ServerError, Unauthorized, utc_now
from .clock import ClockGuard
from .config import Config
from .fsutil import atomic_write_json, read_json
from .health import MpvHealth
from .manifest import Action, EmptyManifest, InvalidManifest, Manifest, parse_manifest
from .mpv_controller import MpvController
from .mpv_ipc import MpvError, MpvIpc
from .schedule import active_entries, local_today, seconds_until_next_evaluation
from .sdnotify import notify
from .state import PlayEntry, StateStore
from .sync import MediaCache, SyncError
from .system import SystemActions

log = logging.getLogger(__name__)

DEFAULT_TZ = ZoneInfo("Europe/Lisbon")
WATCHDOG_PING_S = 10
HEALTH_CHECK_S = 10


class Agent:
    def __init__(
        self,
        config: Config,
        client: ServerClient,
        changes: ChangeSource,
        *,
        now: Callable[[], datetime] = utc_now,
        build_date: date | None = None,
        actions: SystemActions | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.changes = changes
        self.now = now
        self.cache = MediaCache(config)
        self.cache.prepare()
        self.state_store = StateStore(config.state_path)
        self.state = self.state_store.load()
        self.manifest = self._load_manifest()
        self.clock = ClockGuard(config.max_clock_skew_s, build_date)
        self.ipc = MpvIpc(config.mpv_ipc, self._on_mpv_event)
        self.controller = MpvController(
            self.ipc, config.media_dir, config.fallback_image, config.fallback_duration_s
        )
        self._tasks: set[asyncio.Task[Any]] = set()
        # Set by an explicit clear: the fallback replaces the current item at once.
        self._interrupt_pending = False
        self.mpv_ready = asyncio.Event()
        self.health = MpvHealth()
        self.actions = actions or SystemActions()

    # ------------------------------------------------------------- manifest

    def _load_manifest(self) -> Manifest | None:
        raw = read_json(self.config.manifest_path)
        if raw is None:
            return None
        try:
            return parse_manifest(raw)
        except InvalidManifest as exc:
            log.error("Ignoring invalid local manifest.json: %s", exc)
            return None

    def _promote(self, manifest: Manifest) -> None:
        atomic_write_json(self.config.manifest_path, manifest.raw)
        self.manifest = manifest
        self.state.applied_hash = manifest.playlist_hash
        self.state.cleared = manifest.cleared

    def _save(self) -> None:
        self.state_store.save(self.state)

    def _sync_failed(self, message: str) -> bool:
        log.error("Sync failed: %s", message)
        self.state.last_sync_error = f"{self.now().isoformat()} {message}"
        self._save()
        return False

    async def sync_once(self, trigger: SyncTrigger) -> bool:
        """One manifest check. Returns False on any failure (the caller backs off)."""
        etag = None if trigger.full else self.state.seen_hash
        try:
            response = await asyncio.to_thread(self.client.fetch_manifest, etag)
        except Unauthorized as exc:
            return self._sync_failed(f"{exc}; playing the local cache")
        except ServerError as exc:
            return self._sync_failed(str(exc))
        self.clock.observe_server_time(response.server_time, self.now())

        if not response.changed:
            self.state.last_sync_ok = self.now().isoformat()
            self.state.last_sync_error = None
            self._save()
            await self.evaluate()
            return True

        try:
            manifest = parse_manifest(response.raw)
        except EmptyManifest as exc:
            return self._sync_failed(f"refused manifest: {exc}; nothing changed or deleted")
        except InvalidManifest as exc:
            return self._sync_failed(f"refused invalid manifest: {exc}")

        if manifest.action is Action.APPLY:
            try:
                count = await asyncio.to_thread(self.cache.ensure, manifest.items, self.client)
            except (SyncError, ServerError) as exc:
                return self._sync_failed(f"{exc}; keeping the previous playlist")
            if count:
                log.info("Downloaded %d file(s)", count)
            self._promote(manifest)
            log.info(
                "Applied manifest %s (%d items)", manifest.playlist_hash[:12], len(manifest.items)
            )
        elif manifest.action is Action.CLEAR:
            if not self.state.cleared:
                # Explicit admin action: the only case that cuts the current item.
                self._interrupt_pending = True
            self._promote(manifest)
            log.warning("Screen cleared by the dashboard; showing the fallback image now")
        else:
            log.info("No playlist assigned to this player; keeping what is playing")

        self.state.seen_hash = manifest.playlist_hash
        self.state.last_sync_ok = self.now().isoformat()
        self.state.last_sync_error = None
        await self.evaluate()
        self._cleanup()
        self._save()
        return True

    def _cleanup(self) -> None:
        if self.manifest is None:
            return
        referenced = {i.filename for i in self.manifest.items}
        referenced |= {p.filename for p in self.state.playing}
        try:
            self.cache.cleanup(referenced, self.state, self.now())
        except OSError as exc:
            log.error("Media cleanup failed: %s", exc)

    # --------------------------------------------------------------- engine

    def desired_entries(self) -> list[PlayEntry]:
        problem = self.clock.problem(self.now())
        if problem:
            if not self.state.clock_drift:
                log.warning("clock_drift: %s; keeping the last applied list", problem)
            self.state.clock_drift = True
            return list(self.state.playing)
        if self.state.clock_drift:
            log.info("Clock is valid again")
            self.state.clock_drift = False
        if self.manifest is None:
            return list(self.state.playing)
        if self.manifest.cleared:
            return []
        today = local_today(self.now(), self.manifest.tz)
        entries = active_entries(self.manifest.items, today)
        present = [e for e in entries if (self.config.media_dir / e.filename).is_file()]
        if len(present) != len(entries):
            log.error("%d active file(s) missing from disk", len(entries) - len(present))
        return present

    async def evaluate(self) -> None:
        """Recompute the list for today and push it to mpv if it changed."""
        desired = self.desired_entries()
        if desired != self.state.playing:
            log.info(
                "Play list changed: %d entries%s",
                len(desired),
                "" if desired else " (fallback image)",
            )
            self.state.playing = desired
        self._save()
        await self._push()

    async def _push(self) -> None:
        if not self.ipc.connected:
            return
        interrupt = self._interrupt_pending and not self.state.playing
        try:
            await self.controller.apply(self.state.playing, interrupt=interrupt)
        except MpvError as exc:
            log.error("Could not apply the list to mpv: %s", exc)
            return
        self._interrupt_pending = False

    # ------------------------------------------------------------------ mpv

    def _on_mpv_event(self, event: dict[str, Any]) -> None:
        if event.get("event") == "start-file":
            self.health.on_start_file()
            task = asyncio.get_running_loop().create_task(
                self.controller.remove_stale(event.get("playlist_entry_id"))
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def mpv_loop(self) -> None:
        delay = 1.0
        while True:
            try:
                await self.ipc.connect()
            except (OSError, TimeoutError, ValueError) as exc:
                log.warning("mpv not reachable at %s: %s", self.config.mpv_ipc, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 10)
                continue
            delay = 1.0
            log.info("Connected to mpv")
            self.health.on_connect()
            self.controller.reset()  # mpv may have restarted with only the fallback
            await self._push()
            self.mpv_ready.set()
            await self.ipc.closed.wait()
            self.mpv_ready.clear()
            self.health.on_disconnect()
            log.warning("Lost the mpv connection; reconnecting")

    def _current_image_duration(self) -> float | None:
        current = self.health.current_file
        if current is None:
            return None
        if current == self.config.fallback_image.name:
            return float(self.config.fallback_duration_s)
        for entry in self.state.playing:
            if entry.filename == current:
                return entry.duration
        return None

    async def _optional_property(self, name: str) -> Any:
        try:
            return await self.ipc.get_property(name)
        except MpvError:
            return None  # e.g. "property unavailable" while idle

    async def check_mpv_health(self) -> None:
        """Spec 4.6: restart mpv when the screen stops changing; reload an idle mpv."""
        if not self.ipc.connected:
            return
        time_pos = await self._optional_property("time-pos")
        path = await self._optional_property("path")
        idle = bool(await self._optional_property("idle-active"))
        self.health.observe(time_pos, Path(path).name if path else None, idle)
        if idle:
            log.warning("mpv is idle; reloading the play list")
            self.controller.reset()
            await self._push()
        elif self.health.is_stalled(self._current_image_duration()):
            log.error(
                "mpv shows no progress for %.0f s; restarting mpv.service",
                self.health.seconds_without_progress(),
            )
            self.health.mark_restarted()
            if not await self.actions.restart_mpv():
                log.error("Could not restart mpv.service (check the polkit rule)")

    async def health_loop(self) -> None:
        while True:
            await asyncio.sleep(HEALTH_CHECK_S)
            try:
                await self.check_mpv_health()
            except Exception:
                log.exception("Unexpected error while checking mpv health")

    # ---------------------------------------------------------------- loops

    async def sync_loop(self) -> None:
        while True:
            trigger = await self.changes.next_trigger()
            log.debug("Sync triggered (%s, full=%s)", trigger.reason, trigger.full)
            try:
                ok = await self.sync_once(trigger)
            except Exception:
                log.exception("Unexpected sync error")
                ok = False
            self.changes.sync_finished(ok)

    async def schedule_loop(self) -> None:
        while True:
            tz = self.manifest.tz if self.manifest else DEFAULT_TZ
            wait = seconds_until_next_evaluation(self.now(), tz, self.config.reevaluate_interval_s)
            await asyncio.sleep(wait)
            try:
                await self.evaluate()
            except Exception:
                log.exception("Unexpected error while evaluating the playlist")

    async def watchdog_loop(self) -> None:
        while True:
            notify("WATCHDOG=1")
            await asyncio.sleep(WATCHDOG_PING_S)

    async def run(self) -> None:
        await self.evaluate()  # restore the persisted list before any network
        notify("READY=1")
        await asyncio.gather(
            self.mpv_loop(),
            self.sync_loop(),
            self.schedule_loop(),
            self.watchdog_loop(),
            self.health_loop(),
        )
