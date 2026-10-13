"""Agent scenarios (spec §9, phase 1) with a fake dashboard and a fake mpv."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from coral_player.agent import Agent
from coral_player.changes import Backoff, PollingChangeSource, SyncTrigger
from coral_player.client import ServerClient
from coral_player.config import Config
from coral_player.fsutil import read_json
from fake_mpv import FakeMpv
from fake_server import FakeServer
from player_helpers import blob, item_for, make_manifest

POLL = SyncTrigger(full=False, reason="poll")
FULL = SyncTrigger(full=True, reason="full")
NOON = datetime(2026, 10, 13, 11, 0, tzinfo=UTC)  # 12:00 in Lisbon


class Clock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def __call__(self) -> datetime:
        return self.value


class FakeActions:
    """Records restart-player / reboot instead of calling systemctl."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def restart_mpv(self) -> bool:
        self.calls.append("restart_mpv")
        return True

    async def reboot(self) -> bool:
        self.calls.append("reboot")
        return True


class Harness:
    def __init__(self, tmp_path, server: FakeServer, mpv: FakeMpv, clock: Clock) -> None:
        self.tmp_path, self.server, self.mpv, self.clock = tmp_path, server, mpv, clock
        self.agent: Agent | None = None
        self._mpv_task: asyncio.Task | None = None
        self.actions = FakeActions()

    def config(self) -> Config:
        return Config(
            server_url=self.server.url,
            data_dir=self.tmp_path / "var",
            mpv_ipc=self.mpv.address,
            min_free_mb=0,
            http_timeout_s=5,
            fallback_image=self.tmp_path / "fallback.png",
        )

    async def start(self, token: str = "tok") -> Agent:
        if self.agent is not None:
            await self.stop()
        client = ServerClient(self.server.url, token, 5)
        changes = PollingChangeSource(60, 900, Backoff(5, 900))
        self.agent = Agent(
            self.config(),
            client,
            changes,
            now=self.clock,
            build_date=date(2026, 1, 1),
            actions=self.actions,
        )
        await self.agent.evaluate()
        self._mpv_task = asyncio.create_task(self.agent.mpv_loop())
        await asyncio.wait_for(self.agent.mpv_ready.wait(), 5)
        return self.agent

    async def reboot(self, token: str = "tok") -> Agent:
        """Power cut: agent and mpv restart; mpv.service starts with the fallback only."""
        await self.stop()
        port = self.mpv.port
        await self.mpv.stop()
        self.mpv = FakeMpv()
        self.mpv.port = port
        await self.mpv.start([self.fallback])
        return await self.start(token)

    async def stop(self) -> None:
        self._mpv_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._mpv_task
        await self.agent.ipc.close()
        self.agent = None

    def publish(self, *specs, cleared=False, assigned=True):
        """specs: (n, kwargs) pairs or ints; returns the list of file paths."""
        items = []
        for pos, spec in enumerate(specs, start=1):
            n, kwargs = spec if isinstance(spec, tuple) else (spec, {})
            content = blob(n)
            self.server.files[hashlib.sha256(content).hexdigest()] = content
            items.append(item_for(content, pos, **kwargs))
        self.server.manifest = make_manifest(items, cleared=cleared, assigned=assigned)

    def file(self, n: int, ext: str = "png") -> str:
        sha = hashlib.sha256(blob(n)).hexdigest()
        return str(self.tmp_path / "var" / "media" / f"{sha}.{ext}")

    @property
    def fallback(self) -> str:
        return str(self.tmp_path / "fallback.png")

    def media_files(self) -> set[str]:
        media = self.tmp_path / "var" / "media"
        return {p.name for p in media.iterdir() if p.is_file()}

    async def playlist(self) -> list[str]:
        await self.settle()
        return self.mpv.filenames()

    async def settle(self):
        for _ in range(20):
            await asyncio.sleep(0.005)


@pytest.fixture
def scenario(tmp_path):
    """Run an async test body with a started fake server and fake mpv."""

    def runner(body):
        async def main():
            mpv = await FakeMpv().start()
            with FakeServer() as server:
                clock = Clock(NOON)
                server.server_time = clock  # the server agrees with the player's clock
                harness = Harness(tmp_path, server, mpv, clock)
                try:
                    await body(harness)
                finally:
                    if harness.agent is not None:
                        await harness.stop()
                    await mpv.stop()

        asyncio.run(main())

    return runner


# ------------------------------------------------------------------ basics


def test_first_boot_unassigned_shows_fallback(scenario):
    async def body(h):
        h.server.manifest = make_manifest([], assigned=False)
        agent = await h.start()
        assert await agent.sync_once(FULL)
        assert (await h.playlist()) == [h.fallback]
        assert agent.state.playing == []
        assert not (h.tmp_path / "var" / "manifest.json").exists()

    scenario(body)


def test_apply_downloads_then_plays(scenario):
    async def body(h):
        h.publish(1, (2, {"kind": "video", "ext": "mp4", "duration": 12.5}))
        agent = await h.start()
        assert await agent.sync_once(FULL)
        assert (await h.playlist()) == [h.file(1), h.file(2, "mp4")]
        saved = read_json(h.tmp_path / "var" / "manifest.json")
        assert saved["playlist_hash"] == h.server.manifest["playlist_hash"]
        # Unchanged manifest: conditional GET answered with 304.
        assert await agent.sync_once(POLL)
        assert h.server.requests[-1][1]["If-None-Match"] == f'"{saved["playlist_hash"]}"'

    scenario(body)


def test_playlist_update_waits_for_current_item(scenario):
    async def body(h):
        h.publish(1, 2)
        agent = await h.start()
        await agent.sync_once(FULL)
        playing = h.mpv.current_entry
        h.publish(3)
        assert await agent.sync_once(POLL)
        assert h.mpv.current_entry is playing  # not interrupted
        h.mpv.finish_current()
        await h.settle()
        assert (await h.playlist()) == [h.file(3)]

    scenario(body)


# ------------------------------------------------------------- spec §9


def test_corrupted_file_keeps_previous_playlist(scenario):
    async def body(h):
        h.publish(1)
        agent = await h.start()
        await agent.sync_once(FULL)
        old_hash = agent.state.applied_hash

        h.publish(1, 2)
        h.server.corrupt.add(hashlib.sha256(blob(2)).hexdigest())
        assert not await agent.sync_once(POLL)
        assert agent.state.applied_hash == old_hash
        assert "sha256 mismatch" in agent.state.last_sync_error
        assert (await h.playlist()) == [h.file(1)]
        assert Path(h.file(2)).name not in h.media_files()

        h.server.corrupt.clear()  # next try succeeds
        assert await agent.sync_once(POLL)
        assert agent.state.last_sync_error is None

    scenario(body)


def test_empty_manifest_without_flag_is_refused(scenario):
    async def body(h):
        h.publish(1)
        agent = await h.start()
        await agent.sync_once(FULL)
        files_before = h.media_files()
        manifest_before = read_json(h.tmp_path / "var" / "manifest.json")

        h.server.manifest = make_manifest([])
        assert not await agent.sync_once(POLL)
        assert "refused" in agent.state.last_sync_error
        assert read_json(h.tmp_path / "var" / "manifest.json") == manifest_before
        assert h.media_files() == files_before
        assert (await h.playlist()) == [h.file(1)]

    scenario(body)


def test_invalid_manifest_is_refused(scenario):
    async def body(h):
        h.publish(1)
        agent = await h.start()
        await agent.sync_once(FULL)
        h.server.manifest = dict(h.server.manifest, playlist_hash="0" * 64)
        assert not await agent.sync_once(POLL)
        assert "invalid manifest" in agent.state.last_sync_error
        assert (await h.playlist()) == [h.file(1)]

    scenario(body)


def test_cleared_shows_fallback_and_keeps_cache(scenario):
    async def body(h):
        h.publish(1)
        agent = await h.start()
        await agent.sync_once(FULL)
        playing = h.mpv.current_entry
        h.server.manifest = make_manifest([], cleared=True)
        assert await agent.sync_once(POLL)
        assert agent.state.cleared is True
        # Explicit clear: the current item is cut, the fallback shows at once.
        assert (await h.playlist()) == [h.fallback]
        assert h.mpv.current_entry is not playing
        assert Path(h.file(1)).name in h.media_files()  # grace period

        # Restoring the same playlist needs no download.
        h.server.requests.clear()
        h.publish(1)
        assert await agent.sync_once(POLL)
        assert not [p for p in h.server.paths() if "/media/" in p]
        assert agent.state.cleared is False

    scenario(body)


@pytest.mark.parametrize("status", [404, 500, 503])
def test_404_and_5xx_never_clear(scenario, status):
    async def body(h):
        h.publish(1)
        agent = await h.start()
        await agent.sync_once(FULL)
        h.server.manifest_status = status
        assert not await agent.sync_once(FULL)
        assert str(status) in agent.state.last_sync_error
        assert (await h.playlist()) == [h.file(1)]
        assert agent.state.playing

    scenario(body)


def test_unassign_without_clear_keeps_list(scenario):
    async def body(h):
        h.publish(1)
        agent = await h.start()
        await agent.sync_once(FULL)
        h.server.manifest = make_manifest([], assigned=False)
        assert await agent.sync_once(POLL)
        assert (await h.playlist()) == [h.file(1)]
        # After a reboot it still plays the kept list.
        await h.reboot()
        assert (await h.playlist()) == [h.file(1)]

    scenario(body)


def test_unassign_with_clear_shows_fallback(scenario):
    async def body(h):
        h.publish(1)
        agent = await h.start()
        await agent.sync_once(FULL)
        h.server.manifest = make_manifest([], assigned=False, cleared=True)
        assert await agent.sync_once(POLL)
        assert (await h.playlist()) == [h.fallback]  # immediate, like Clear screen

    scenario(body)


def test_no_valid_items_today_shows_fallback(scenario):
    async def body(h):
        h.publish((1, {"start": "2026-11-01"}), (2, {"end": "2026-10-01"}))
        agent = await h.start()
        assert await agent.sync_once(FULL)
        assert (await h.playlist()) == [h.fallback]
        # Both files are still downloaded: the first one starts on 1 Nov.
        assert len(h.media_files()) == 2

    scenario(body)


def test_item_expires_at_midnight_without_sync(scenario):
    async def body(h):
        h.publish(1, (2, {"end": "2026-10-13"}))
        agent = await h.start()
        await agent.sync_once(FULL)
        assert (await h.playlist()) == [h.file(1), h.file(2)]
        requests = len(h.server.requests)

        h.clock.value = datetime(2026, 10, 13, 23, 0, 1, tzinfo=UTC)  # 00:00:01 Lisbon
        await agent.evaluate()
        h.mpv.finish_current()
        await h.settle()
        assert (await h.playlist()) == [h.file(1)]
        assert len(h.server.requests) == requests  # no network involved

    scenario(body)


def test_item_starts_at_midnight_without_sync(scenario):
    async def body(h):
        h.publish(1, (2, {"start": "2026-10-14"}))
        agent = await h.start()
        await agent.sync_once(FULL)
        assert (await h.playlist()) == [h.file(1)]
        h.clock.value += timedelta(hours=13)  # next day in Lisbon
        await agent.evaluate()
        h.mpv.finish_current()
        await h.settle()
        assert (await h.playlist()) == [h.file(1), h.file(2)]

    scenario(body)


def test_invalid_clock_keeps_last_list_and_reports(scenario):
    async def body(h):
        h.publish(1, (2, {"end": "2026-10-13"}))
        agent = await h.start()
        await agent.sync_once(FULL)

        # Power cut, boot without network: clock back to 1970.
        h.clock.value = datetime(1970, 1, 1, tzinfo=UTC)
        agent = await h.reboot()
        assert agent.state.clock_drift is True
        assert (await h.playlist()) == [h.file(1), h.file(2)]

        # Network back but the clock is still wrong vs the server Date: keep the list.
        h.server.server_time = NOON + timedelta(days=1)
        await agent.sync_once(POLL)
        assert agent.state.clock_drift is True
        assert (await h.playlist()) == [h.file(1), h.file(2)]

        # chrony fixes the clock: dates apply again (item 2 expired yesterday).
        h.clock.value = NOON + timedelta(days=1)
        await agent.sync_once(POLL)
        assert agent.state.clock_drift is False
        h.mpv.finish_current()
        await h.settle()
        assert (await h.playlist()) == [h.file(1)]

    scenario(body)


def test_revoked_token_keeps_playing_cache(scenario):
    async def body(h):
        h.publish(1)
        agent = await h.start()
        await agent.sync_once(FULL)
        agent = await h.reboot(token="revoked")
        assert not await agent.sync_once(POLL)
        assert "401" in agent.state.last_sync_error
        assert (await h.playlist()) == [h.file(1)]

    scenario(body)


def test_power_cut_restores_last_list_before_network(scenario):
    async def body(h):
        h.publish(1, 2)
        agent = await h.start()
        await agent.sync_once(FULL)
        h.server.manifest_status = 500  # VPS down after the reboot
        await h.reboot()
        assert (await h.playlist()) == [h.file(1), h.file(2)]

    scenario(body)


def test_mpv_restart_reapplies_list(scenario):
    async def body(h):
        h.publish(1)
        agent = await h.start()
        await agent.sync_once(FULL)
        # mpv dies and comes back with only the fallback (as mpv.service starts it).
        port = h.mpv.port
        await h.mpv.stop()
        await asyncio.wait_for(agent.ipc.closed.wait(), 2)
        agent.mpv_ready.clear()
        new_mpv = FakeMpv()
        new_mpv.port = port
        await new_mpv.start([h.fallback])
        h.mpv = new_mpv
        await asyncio.wait_for(agent.mpv_ready.wait(), 10)
        await h.settle()
        assert new_mpv.current_entry.filename == h.file(1)

    scenario(body)


def test_disk_full_keeps_previous_list(scenario, tmp_path):
    async def body(h):
        h.publish(1)
        agent = await h.start()
        await agent.sync_once(FULL)
        h.publish(1, 2)
        agent.cache.config = Config(
            server_url=h.server.url, data_dir=tmp_path / "var", min_free_mb=10**9
        )
        assert not await agent.sync_once(POLL)
        assert "disk space" in agent.state.last_sync_error
        assert (await h.playlist()) == [h.file(1)]

    scenario(body)


def test_repeated_cleared_manifest_does_not_cut_again(scenario):
    async def body(h):
        h.publish(1)
        agent = await h.start()
        await agent.sync_once(FULL)
        h.server.manifest = make_manifest([], cleared=True)
        await agent.sync_once(POLL)
        starts = len(h.mpv.starts)
        await agent.sync_once(FULL)  # same cleared manifest again (periodic full check)
        await h.settle()
        assert len(h.mpv.starts) == starts

    scenario(body)


def test_no_valid_items_today_waits_for_current_item(scenario):
    """Only an explicit clear cuts the current item; date expiry does not."""

    async def body(h):
        h.publish((1, {"end": "2026-10-13"}))
        agent = await h.start()
        await agent.sync_once(FULL)
        playing = h.mpv.current_entry
        h.clock.value += timedelta(hours=13)  # item expired at midnight
        await agent.evaluate()
        await h.settle()
        assert h.mpv.current_entry is playing
        assert (await h.playlist()) == [h.file(1), h.fallback]
        h.mpv.finish_current()
        assert (await h.playlist()) == [h.fallback]

    scenario(body)


# ------------------------------------------------------------- mpv health


def test_stuck_mpv_is_restarted_and_reported(scenario):
    async def body(h):
        h.publish(1, (2, {"kind": "video", "ext": "mp4", "duration": 12.5}))
        agent = await h.start()
        await agent.sync_once(FULL)
        await h.settle()
        await agent.check_mpv_health()  # first look: records the current file
        assert agent.health.report().state == "playing"

        agent.health._last_progress -= 1000  # nothing moved for a long time
        await agent.check_mpv_health()
        assert h.actions.calls == ["restart_mpv"]
        report = agent.health.report()
        assert (report.state, report.restarts) == ("stuck", 1)

    scenario(body)


def test_advancing_mpv_is_not_restarted(scenario):
    async def body(h):
        h.publish((1, {"kind": "video", "ext": "mp4", "duration": 600}))
        agent = await h.start()
        await agent.sync_once(FULL)
        for pos in (1.0, 2.0, 3.0):
            h.mpv.time_pos = pos
            agent.health._last_progress -= 1000
            await agent.check_mpv_health()
        assert h.actions.calls == []

    scenario(body)


def test_idle_mpv_gets_the_list_again(scenario):
    async def body(h):
        h.publish(1)
        agent = await h.start()
        await agent.sync_once(FULL)
        h.mpv.playlist.clear()
        h.mpv.current = None  # mpv went idle (e.g. the playlist was cleared by hand)
        await agent.check_mpv_health()
        assert (await h.playlist()) == [h.file(1)]
        assert h.actions.calls == []

    scenario(body)
