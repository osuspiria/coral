from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest
from coral_dashboard.auth import hash_token

AUTH = {"Authorization": "Bearer rel-token"}
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
TARBALL = bytes(range(256)) * 50
SIGNATURE = "untrusted comment: test\nRWQ...\ntrusted comment: coral-player 0.3.0\nabc\n"


@pytest.fixture
def player(app, app_store, settings):
    app.state.coral.clock = lambda: NOW
    (settings.releases_dir / "coral-player-0.3.0.tar.gz").write_bytes(TARBALL)
    app_store.add_release("0.3.0", hashlib.sha256(TARBALL).hexdigest(), len(TARBALL), SIGNATURE)
    return app_store.create_player("pombal", hash_token("rel-token"))


def heartbeat(client, **update):
    body = {
        "agent_version": "0.2.0",
        "player_time": "x",
        "mpv": {"state": "playing"},
        "sync": {},
        "update": update or {"state": "idle"},
    }
    return client.post("/api/v1/heartbeat", json=body, headers=AUTH).json()


def test_release_download_requires_token(client, player):
    assert client.get("/api/v1/releases/0.3.0").status_code == 401
    assert client.get("/api/v1/releases/0.3.0/signature").status_code == 401


def test_release_download_and_range(client, player):
    full = client.get("/api/v1/releases/0.3.0", headers=AUTH)
    assert full.status_code == 200 and full.content == TARBALL
    part = client.get("/api/v1/releases/0.3.0", headers={**AUTH, "Range": "bytes=100-"})
    assert part.status_code == 206 and part.content == TARBALL[100:]
    sig = client.get("/api/v1/releases/0.3.0/signature", headers=AUTH)
    assert sig.text == SIGNATURE


@pytest.mark.parametrize("version", ["0.9.9", "..%2F..%2Fetc%2Fpasswd", "latest"])
def test_unknown_or_bad_versions_404(client, player, version):
    assert client.get(f"/api/v1/releases/{version}", headers=AUTH).status_code == 404
    assert client.get(f"/api/v1/releases/{version}/signature", headers=AUTH).status_code == 404


def test_heartbeat_sends_desired_version_and_records_failure(client, player, app_store):
    assert heartbeat(client)["desired_version"] is None
    app_store.set_channel_version("stable", "0.3.0")
    assert heartbeat(client)["desired_version"] == "0.3.0"
    answer = heartbeat(client, state="rolled_back", version="0.3.0", last_error="no commit")
    assert answer["desired_version"] is None  # the failed version is not requested again
    assert app_store.get_player(player)["failed_version"] == "0.3.0"
