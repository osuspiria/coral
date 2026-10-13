from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from coral_player.mpv_controller import MpvController
from coral_player.mpv_ipc import MpvError, MpvIpc
from coral_player.state import PlayEntry
from fake_mpv import FakeMpv

MEDIA = Path("/var/lib/signage/media")
FALLBACK = Path("/opt/signage/current/player/assets/fallback.png")


def entry(n: int, kind: str = "image") -> PlayEntry:
    ext = "png" if kind == "image" else "mp4"
    return PlayEntry(f"{n:064x}", ext, kind, 10 if kind == "image" else None)


def path(n: int, ext: str = "png") -> str:
    return str(MEDIA / f"{n:064x}.{ext}")


async def setup(initial=None):
    mpv = await FakeMpv().start(initial)
    controller: MpvController | None = None

    def on_event(event):
        if event.get("event") == "start-file":
            asyncio.get_running_loop().create_task(
                controller.remove_stale(event.get("playlist_entry_id"))
            )

    ipc = MpvIpc(mpv.address, on_event)
    await ipc.connect()
    controller = MpvController(ipc, MEDIA, FALLBACK, 30)
    return mpv, ipc, controller


async def settle():
    for _ in range(20):
        await asyncio.sleep(0.005)


def run(coro):
    asyncio.run(coro)


def test_idle_mpv_starts_list():
    async def scenario():
        mpv, ipc, ctl = await setup()
        await ctl.apply([entry(1), entry(2, "video")])
        assert mpv.filenames() == [path(1), path(2, "mp4")]
        assert mpv.current_entry.filename == path(1)
        assert mpv.playlist[0].options == "image-display-duration=10"
        assert mpv.playlist[1].options == ""
        await ipc.close()
        await mpv.stop()

    run(scenario())


def test_new_list_does_not_interrupt_current_item():
    """Invariant 3: applying a new list never restarts the item on screen."""

    async def scenario():
        mpv, ipc, ctl = await setup()
        await ctl.apply([entry(1), entry(2, "video")])
        mpv.finish_current()  # now playing the video (entry 2)
        await settle()
        playing = mpv.current_entry
        starts_before = len(mpv.starts)

        await ctl.apply([entry(3), entry(4)])
        await settle()
        # Same entry still current, nothing (re)started, no replace/loadlist sent.
        assert mpv.current_entry is playing
        assert len(mpv.starts) == starts_before
        assert all("loadlist" not in str(c) and "replace" not in str(c) for c in mpv.commands)
        assert mpv.filenames() == [path(2, "mp4"), path(3), path(4)]

        # When the video ends, the new list starts and the old entry is removed.
        mpv.finish_current()
        await settle()
        assert mpv.current_entry.filename == path(3)
        assert mpv.filenames() == [path(3), path(4)]
        # And the list loops over the new items only.
        mpv.finish_current()
        mpv.finish_current()
        await settle()
        assert mpv.current_entry.filename == path(3)
        await ipc.close()
        await mpv.stop()

    run(scenario())


def test_two_quick_updates_keep_current_item():
    async def scenario():
        mpv, ipc, ctl = await setup()
        await ctl.apply([entry(1)])
        playing = mpv.current_entry
        await ctl.apply([entry(2)])
        await ctl.apply([entry(3), entry(4)])
        await settle()
        assert mpv.current_entry is playing
        assert mpv.filenames() == [path(1), path(3), path(4)]
        mpv.finish_current()
        await settle()
        assert mpv.filenames() == [path(3), path(4)]
        await ipc.close()
        await mpv.stop()

    run(scenario())


def test_same_list_is_not_reapplied():
    async def scenario():
        mpv, ipc, ctl = await setup()
        assert await ctl.apply([entry(1)])
        count = len(mpv.commands)
        assert not await ctl.apply([entry(1)])
        assert len(mpv.commands) == count
        await ipc.close()
        await mpv.stop()

    run(scenario())


def test_empty_list_shows_fallback_after_current_item():
    """Invariant 1: never a black screen. An empty list means the fallback image."""

    async def scenario():
        mpv, ipc, ctl = await setup()
        await ctl.apply([entry(1)])
        await ctl.apply([])
        assert mpv.current_entry.filename == path(1)  # not interrupted
        mpv.finish_current()
        await settle()
        assert mpv.filenames() == [str(FALLBACK)]
        assert mpv.playlist[0].options == "image-display-duration=30"
        await ipc.close()
        await mpv.stop()

    run(scenario())


def test_fallback_is_left_immediately():
    """mpv.service starts with the fallback; real content replaces it at once."""

    async def scenario():
        mpv, ipc, ctl = await setup([str(FALLBACK)])
        await ctl.apply([entry(1), entry(2)])
        await settle()
        assert mpv.current_entry.filename == path(1)
        assert mpv.filenames() == [path(1), path(2)]
        await ipc.close()
        await mpv.stop()

    run(scenario())


def test_reset_after_reconnect_reapplies():
    async def scenario():
        mpv, ipc, ctl = await setup()
        await ctl.apply([entry(1)])
        ctl.reset()
        assert await ctl.apply([entry(1)])
        await ipc.close()
        await mpv.stop()

    run(scenario())


def test_disconnect_raises_and_status():
    async def scenario():
        mpv, ipc, ctl = await setup()
        assert await ctl.status() == {"current_file": None}
        await ctl.apply([entry(1)])
        assert (await ctl.status())["current_file"] == f"{1:064x}.png"
        await mpv.stop()
        await asyncio.wait_for(ipc.closed.wait(), 2)
        with pytest.raises(MpvError):
            await ctl.apply([entry(2)])

    run(scenario())
