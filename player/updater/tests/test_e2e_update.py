"""End to end (phase 3 acceptance, automated): real dashboard + real minisign +
updater + agent update client + the real selftest of the built release.

A good release is installed and committed; a deliberately broken one is
installed, never committed and rolled back automatically, after which the
server stops offering it. POSIX and minisign only (run on Linux/WSL).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import shutil
import socket
import subprocess
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix" or shutil.which("minisign") is None,
    reason="needs POSIX and the minisign CLI",
)

import release  # noqa: E402
import uvicorn  # noqa: E402
from coral_dashboard.auth import hash_token  # noqa: E402
from coral_dashboard.db import connect  # noqa: E402
from coral_dashboard.main import create_app  # noqa: E402
from coral_dashboard.repo import Store  # noqa: E402
from coral_player.client import ServerClient  # noqa: E402
from coral_player.update import UpdateClient  # noqa: E402
from coral_updater import fsops  # noqa: E402
from coral_updater.updater import COMMIT_DEADLINE_S, Paths, Updater  # noqa: E402
from dash_helpers import make_settings  # noqa: E402
from fake_mpv import FakeMpv  # noqa: E402

TOKEN = "e2e-update-token"  # noqa: S105


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class MpvThread:
    """A fake mpv on its own event loop, so the selftest subprocess can reach it."""

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.mpv = FakeMpv()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        asyncio.run_coroutine_threadsafe(self.mpv.start(), self.loop).result(5)

    def stop(self):
        asyncio.run_coroutine_threadsafe(self.mpv.stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)


@pytest.fixture
def world(tmp_path):
    # Signing key (development machine side).
    pub, sec = tmp_path / "release.pub", tmp_path / "release.key"
    subprocess.run(["minisign", "-G", "-W", "-p", pub, "-s", sec], check=True, capture_output=True)

    # Dashboard.
    settings = make_settings(tmp_path / "dashboard")
    app = create_app(settings)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    url = f"http://127.0.0.1:{port}"
    conn = connect(settings.db_path)
    store = Store(conn, app.state.coral.ctx)
    player = store.create_player("pombal", hash_token(TOKEN))

    # The Pi's filesystem, under a fake root.
    paths = Paths(tmp_path / "pi")
    old = paths.releases / "0.2.0"
    (old / "player").mkdir(parents=True)
    os.symlink(old, paths.current)
    paths.etc.mkdir(parents=True)
    token = paths.etc / "token"
    token.write_text(TOKEN)
    shutil.copy(pub, paths.etc / "release-pubkey.pub")
    data_dir = paths.root / "var/lib/signage"
    data_dir.mkdir(parents=True)
    mpv = MpvThread()
    (paths.etc / "config.toml").write_text(
        f'server_url = "{url}"\n'
        f'token_file = "{token}"\n'
        f'data_dir = "{data_dir}"\n'
        f'mpv_ipc = "{mpv.mpv.address}"\n'
    )

    def publish(version, broken=False):
        out = tmp_path / "dist"
        tarball = release.build_tarball(
            release.ROOT, version, out, broken=broken, build_date=dt.date(2026, 10, 14)
        )
        signature = release.sign(tarball, sec, version)
        (settings.releases_dir / tarball.name).write_bytes(tarball.read_bytes())
        import hashlib

        store.add_release(
            version,
            hashlib.sha256(tarball.read_bytes()).hexdigest(),
            tarball.stat().st_size,
            signature.read_text(),
        )

    calls = []

    def runner(argv):
        calls.append(list(argv))
        if "coral_player.selftest" in argv:
            argv = argv[argv.index("--") + 1 :]  # drop "runuser -u signage --"
            return subprocess.run(argv, check=False).returncode
        return 0  # systemctl restart

    clock = {"t": 1000.0}
    updater = Updater(
        paths, run=runner, monotonic=lambda: clock["t"], python=sys.executable, boot=lambda: "boot"
    )

    class World:
        pass

    w = World()
    w.url, w.store, w.player, w.paths, w.data_dir = url, store, player, paths, data_dir
    w.publish, w.updater, w.clock, w.calls = publish, updater, clock, calls
    yield w
    mpv.stop()
    conn.close()
    server.should_exit = True


def heartbeat(w, client: UpdateClient, running: str):
    """What the agent does on each heartbeat, reduced to the update protocol."""
    body = {
        "agent_version": running,
        "player_time": "x",
        "mpv": {"state": "playing", "progressing": True},
        "sync": {},
        "update": client.report(),
    }
    response = ServerClient(w.url, TOKEN, 5).post_heartbeat(body)
    client.want(response.desired_version)
    client.maybe_commit(heartbeat_accepted=True, playing=True)
    return response


def test_good_update_then_broken_update_rolls_back(world):
    w = world
    state = w.paths.state

    # --- good update: 0.3.0 on the stable channel -------------------------
    w.publish("0.3.0")
    w.store.set_channel_version("stable", "0.3.0")
    old_agent = UpdateClient(w.data_dir, "0.2.0", state)
    assert heartbeat(w, old_agent, "0.2.0").desired_version == "0.3.0"
    w.updater.tick()  # download, verify, extract, real selftest, switch
    assert fsops.link_target(w.paths.current) == w.paths.releases / "0.3.0"
    assert (w.paths.releases / "0.3.0/player/coral_player/_build.py").exists()
    new_agent = UpdateClient(w.data_dir, "0.3.0", state)  # the restarted agent
    heartbeat(w, new_agent, "0.3.0")  # accepted + playing -> commit
    w.updater.tick()
    assert fsops.read_json(state / "status.json")["state"] == "committed"
    # The target stays 0.3.0; running it, the agent asks for nothing more.
    assert heartbeat(w, new_agent, "0.3.0").desired_version == "0.3.0"
    assert not (w.data_dir / "update/request.json").exists()

    # --- broken update: agent exits, never commits -> automatic rollback ---
    w.publish("0.3.1", broken=True)
    w.store.set_channel_version("stable", "0.3.1")
    assert heartbeat(w, new_agent, "0.3.0").desired_version == "0.3.1"
    w.updater.tick()
    assert fsops.link_target(w.paths.current) == w.paths.releases / "0.3.1"
    # The broken agent never sends a heartbeat. The deadline passes.
    w.clock["t"] += COMMIT_DEADLINE_S + 1
    w.updater.tick()
    assert fsops.link_target(w.paths.current) == w.paths.releases / "0.3.0"
    assert fsops.read_json(state / "status.json")["state"] == "rolled_back"

    # The restored 0.3.0 agent reports it; the server stops offering 0.3.1.
    restored = UpdateClient(w.data_dir, "0.3.0", state)
    answer = heartbeat(w, restored, "0.3.0")
    assert answer.desired_version is None
    assert w.store.get_player(w.player)["failed_version"] == "0.3.1"
    assert not (w.data_dir / "update/request.json").exists()


def test_tampered_release_is_rejected(world):
    w = world
    w.publish("0.3.0")
    tarball = w.paths.root.parent / "dashboard/releases/coral-player-0.3.0.tar.gz"
    tarball.write_bytes(tarball.read_bytes()[:-10] + b"0123456789")  # VPS compromised
    w.store.set_channel_version("stable", "0.3.0")
    heartbeat(w, UpdateClient(w.data_dir, "0.2.0", w.paths.state), "0.2.0")
    w.updater.tick()
    status = fsops.read_json(w.paths.state / "status.json")
    assert (status["state"], status["version"]) == ("rejected", "0.3.0")
    assert fsops.link_target(w.paths.current) == w.paths.releases / "0.2.0"
    assert not any("coral_player.selftest" in c for c in w.calls)
