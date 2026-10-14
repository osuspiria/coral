"""Validates the playlist strategy against a real mpv (skipped when mpv is absent).

This is the check the spec leaves open (§4.5, §11): applying a new list must not
restart the item on screen. It also runs without pytest, so it can be executed on
the Pi itself against the installed mpv:

    PYTHONPATH=player python3 player/tests/test_mpv_real.py [--vo=null]

It needs mpv and ffmpeg (to generate the sample media).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

try:
    import pytest
except ImportError:  # standalone run on the Pi
    pytest = None

from coral_player.mpv_controller import MpvController
from coral_player.mpv_ipc import MpvIpc
from coral_player.state import PlayEntry

AVAILABLE = bool(shutil.which("mpv") and shutil.which("ffmpeg") and hasattr(socket, "AF_UNIX"))


def _ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-v", "error", "-y", *args], check=True, timeout=120)


def make_media(media: Path) -> dict[str, PlayEntry]:
    """Two short videos and two images, named <name>.<ext> for readability."""
    media.mkdir(parents=True, exist_ok=True)
    entries = {}
    for name, seconds in (("va", 4), ("vd", 2)):
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"testsrc=s=320x240:r=25:d={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(media / f"{name}.mp4"),
        )
        entries[name] = PlayEntry(name, "mp4", "video", None)
    for name, color in (("ib", "blue"), ("ic", "green")):
        _ffmpeg(
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s=320x240",
            "-frames:v",
            "1",
            str(media / f"{name}.png"),
        )
        entries[name] = PlayEntry(name, "png", "image", 1)
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "color=c=black:s=320x240",
        "-frames:v",
        "1",
        str(media / "fallback.png"),
    )
    return entries


async def validate(workdir: Path, vo: str = "null") -> list[str]:
    """Run the scenario. Returns the list of checks that passed; raises on failure."""
    media = workdir / "media"
    entries = make_media(media)
    sock = workdir / "mpv.sock"
    fallback = media / "fallback.png"
    proc = subprocess.Popen(
        [
            "mpv",
            "--idle=yes",
            f"--vo={vo}",
            "--ao=null",
            "--loop-playlist=inf",
            "--image-display-duration=5",  # global; the list uses 1 s per file
            "--no-terminal",
            f"--input-ipc-server={sock}",
            str(fallback),
        ],
    )
    passed: list[str] = []
    starts: list[int] = []
    try:
        for _ in range(100):
            if sock.exists():
                break
            await asyncio.sleep(0.05)
        controller: MpvController | None = None
        tasks = set()

        def on_event(event):
            if event.get("event") == "start-file":
                starts.append(event.get("playlist_entry_id"))
                task = asyncio.get_running_loop().create_task(
                    controller.remove_stale(event.get("playlist_entry_id"))
                )
                tasks.add(task)

        ipc = MpvIpc(f"unix:{sock}", on_event)
        await ipc.connect()
        controller = MpvController(ipc, media, fallback, 2)

        async def names() -> list[str]:
            playlist = await ipc.get_property("playlist")
            return [Path(e["filename"]).name for e in playlist]

        async def current() -> str:
            return Path(await ipc.get_property("path")).name

        async def wait_for(predicate, timeout=10.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if await predicate():
                    return
                await asyncio.sleep(0.05)
            raise AssertionError(f"timed out; playlist={await names()}")

        # 1. The fallback is left at once for real content.
        await controller.apply([entries["va"], entries["ib"]])

        async def left_fallback():
            return await names() == ["va.mp4", "ib.png"]

        await wait_for(left_fallback, 5)
        assert await current() == "va.mp4"
        passed.append("fallback replaced immediately, stale fallback entry removed")

        # 2. A new list while the video plays: the video is not restarted.
        await asyncio.sleep(1.5)
        pos_before = await ipc.get_property("time-pos")
        starts_before = len(starts)
        await controller.apply([entries["ic"], entries["vd"]])
        await asyncio.sleep(0.3)
        pos_after = await ipc.get_property("time-pos")
        assert await current() == "va.mp4", "the current item changed"
        assert pos_after >= pos_before, f"time-pos went back: {pos_before} -> {pos_after}"
        assert len(starts) == starts_before, "a file was (re)started by the update"
        assert await names() == ["va.mp4", "ic.png", "vd.mp4"]
        passed.append(f"update kept the video playing (time-pos {pos_before:.2f}->{pos_after:.2f})")

        # 3. When the video ends, the new list starts and the old entry is gone.
        async def new_list_playing():
            return await names() == ["ic.png", "vd.mp4"]

        await wait_for(new_list_playing, 8)
        assert await current() == "ic.png"
        passed.append("old entry removed after it finished; new list playing")

        # 4. Per-file image duration (1 s) is honoured, then the list loops.
        t0 = time.monotonic()

        async def on_video():
            return await current() == "vd.mp4"

        await wait_for(on_video, 5)
        shown = time.monotonic() - t0
        assert shown < 3.5, f"image shown {shown:.2f}s (per-file option ignored?)"

        async def looped():
            return await current() == "ic.png"

        await wait_for(looped, 6)
        passed.append(f"per-file image-display-duration honoured (~{shown:.1f}s) and list loops")

        # 5. Empty list -> fallback after the current item.
        await controller.apply([])

        async def on_fallback():
            return await names() == ["fallback.png"]

        await wait_for(on_fallback, 8)
        passed.append("empty list shows the fallback after the current item")
        await ipc.close()
        return passed
    finally:
        proc.terminate()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(5)


if pytest is not None:

    @pytest.mark.skipif(not AVAILABLE, reason="mpv/ffmpeg not installed or no AF_UNIX")
    def test_real_mpv_does_not_interrupt_current_item(tmp_path):
        passed = asyncio.run(validate(tmp_path))
        assert len(passed) == 5


if __name__ == "__main__":
    vo = next((a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--vo=")), "null")
    version = subprocess.run(["mpv", "--version"], capture_output=True, text=True).stdout
    print(version.splitlines()[0] if version else "mpv: unknown version")
    with tempfile.TemporaryDirectory() as tmp:
        try:
            for line in asyncio.run(validate(Path(tmp), vo)):
                print("OK  ", line)
        except AssertionError as exc:
            print("FAIL", exc)
            sys.exit(1)
    print("All checks passed.")
    sys.stdout.flush()  # os._exit skips buffer flushing when piped
    os._exit(0)
