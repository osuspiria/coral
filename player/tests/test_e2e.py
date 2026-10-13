"""End to end: the real dashboard (uvicorn) + the agent + a fake mpv.

Acceptance criterion of phase 1 (spec §10.3), automated: the player plays the
assigned playlist and follows changes to it.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import socket
import threading
import time

import pytest
import uvicorn
from coral_dashboard.auth import hash_token
from coral_dashboard.db import connect
from coral_dashboard.main import create_app
from coral_dashboard.repo import Store
from coral_player.agent import Agent
from coral_player.changes import Backoff, HeartbeatChangeSource
from coral_player.client import ServerClient
from coral_player.config import Config
from dash_helpers import make_settings
from fake_mpv import FakeMpv
from player_helpers import blob


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def dashboard(tmp_path):
    settings = make_settings(tmp_path / "dashboard")
    app = create_app(settings)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    conn = connect(settings.db_path)
    yield f"http://127.0.0.1:{port}", Store(conn, app.state.coral.ctx), settings
    conn.close()
    server.should_exit = True
    thread.join(5)


def add_file(store: Store, settings, n: int) -> int:
    content = blob(n)
    sha = hashlib.sha256(content).hexdigest()
    (settings.media_dir / f"{sha}.png").write_bytes(content)
    return store.add_media(
        sha256=sha,
        stored_name=f"{sha}.png",
        original_name=f"{n}.png",
        kind="image",
        size=len(content),
        duration_s=None,
        codec="png",
        width=8,
        height=8,
    )


def test_player_follows_assigned_playlist(dashboard, tmp_path):
    url, store, settings = dashboard
    playlist = store.create_playlist("Loja")
    store.add_item(playlist, add_file(store, settings, 1), image_duration_s=8)
    store.add_item(playlist, add_file(store, settings, 2))
    player = store.create_player("pombal", hash_token("e2e-token"), playlist_id=playlist)

    async def scenario():
        mpv = await FakeMpv().start()
        config = Config(
            server_url=url,
            data_dir=tmp_path / "pi",
            mpv_ipc=mpv.address,
            min_free_mb=0,
            full_check_interval_s=3600,
            fallback_image=tmp_path / "fallback.png",
        )
        changes = HeartbeatChangeSource(3600, Backoff(0.1, 0.5))
        agent = Agent(
            config, ServerClient(url, "e2e-token", 5), changes, heartbeat_interval_override=0.2
        )
        task = asyncio.create_task(agent.run())
        media = tmp_path / "pi" / "media"

        async def wait_for(predicate, timeout=15):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if predicate():
                    return
                await asyncio.sleep(0.05)
            raise AssertionError(f"timed out; mpv playlist: {mpv.filenames()}")

        def names():
            return [n.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for n in mpv.filenames()]

        sha = [hashlib.sha256(blob(n)).hexdigest() for n in (1, 2, 3)]
        try:
            # 1. The assigned playlist plays.
            await wait_for(lambda: names() == [f"{sha[0]}.png", f"{sha[1]}.png"])
            assert mpv.playlist[0].options == "image-display-duration=8"
            assert mpv.playlist[1].options == "image-display-duration=10"
            assert (media / f"{sha[0]}.png").read_bytes() == blob(1)

            # 2. Editing the playlist reaches the screen after the current item.
            store.add_item(playlist, add_file(store, settings, 3))
            await wait_for(lambda: len(mpv.filenames()) == 4)  # current kept + 3 new
            mpv.finish_current()
            await wait_for(lambda: names() == [f"{s}.png" for s in sha])

            # 3. Clear screen -> fallback at once (explicit clear cuts the current item).
            store.set_cleared(player, True)
            fallback = str(tmp_path / "fallback.png")
            await wait_for(lambda: mpv.filenames() == [fallback])

            # 4. Resume -> content again, the fallback is left at once.
            store.set_cleared(player, False)
            await wait_for(lambda: names() == [f"{s}.png" for s in sha])
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            await agent.ipc.close()
            await mpv.stop()

    asyncio.run(scenario())
