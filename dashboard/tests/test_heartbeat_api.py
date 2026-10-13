from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from coral_dashboard.auth import hash_token
from dash_helpers import add_media

AUTH = {"Authorization": "Bearer hb-token"}
NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


def body(**overrides):
    data = {
        "agent_version": "0.2.0",
        "player_time": "2026-10-14T11:00:00+01:00",
        "playlist_hash_applied": "a" * 64,
        "playlist_hash_seen": "a" * 64,
        "cleared": False,
        "clock_drift": False,
        "mpv": {"state": "playing", "current_file": "x.png", "progressing": True, "restarts": 0},
        "sync": {"last_ok": "2026-10-14T10:59:00+00:00", "last_error": None},
        "update": {"state": "idle", "last_error": None},
        "system": {"disk_free_mb": 180000, "temp_c": 51.2, "uptime_s": 86400},
        "acked_commands": [],
    }
    data.update(overrides)
    return data


@pytest.fixture
def player(app, app_store):
    app.state.coral.clock = lambda: NOW
    playlist = app_store.create_playlist("Loja")
    app_store.add_item(playlist, add_media(app_store, 1))
    return app_store.create_player("pombal", hash_token("hb-token"), playlist_id=playlist)


def test_heartbeat_requires_token(client, player):
    assert client.post("/api/v1/heartbeat", json=body()).status_code == 401


def test_heartbeat_records_state_and_answers(client, player, app_store):
    response = client.post("/api/v1/heartbeat", json=body(extra_field="ignored"), headers=AUTH)
    assert response.status_code == 200, response.text
    data = response.json()
    row = app_store.get_player(player)
    assert data == {
        "server_time": NOW.isoformat(),
        "playlist_hash": app_store.player_etag(row),
        "desired_version": None,
        "commands": [],
        "heartbeat_interval_s": 60,
    }
    assert row["last_seen"] == NOW.isoformat()
    stored = json.loads(row["last_state"])
    assert stored["system"]["temp_c"] == 51.2
    assert "extra_field" not in stored


@pytest.mark.parametrize(
    "bad",
    [
        {"mpv": {"state": "rm -rf", "progressing": True}},
        {"agent_version": "x" * 100},
        {"acked_commands": list(range(500))},
        {"system": {"disk_free_mb": -1}},
    ],
)
def test_invalid_heartbeat_is_rejected(client, player, app_store, bad):
    response = client.post("/api/v1/heartbeat", json=body(**bad), headers=AUTH)
    assert response.status_code == 422
    assert app_store.get_player(player)["last_seen"] is None


def test_oversized_heartbeat_is_rejected(client, player):
    payload = json.dumps(body(padding="x" * 20000))
    response = client.post(
        "/api/v1/heartbeat",
        content=payload,
        headers={**AUTH, "Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_commands_delivered_and_acked(client, player, app_store):
    sync_id = app_store.queue_command(player, "sync-now", NOW)
    first = client.post("/api/v1/heartbeat", json=body(), headers=AUTH).json()
    assert first["commands"] == [{"id": sync_id, "name": "sync-now"}]
    # Not acked yet: delivered again.
    again = client.post("/api/v1/heartbeat", json=body(), headers=AUTH).json()
    assert again["commands"] == [{"id": sync_id, "name": "sync-now"}]
    acked = client.post(
        "/api/v1/heartbeat", json=body(acked_commands=[sync_id]), headers=AUTH
    ).json()
    assert acked["commands"] == []
    assert app_store.list_commands(player)[0]["acked_at"] == NOW.isoformat()


def test_old_command_expires_instead_of_being_delivered(client, player, app_store, app):
    app_store.queue_command(player, "reboot", NOW - timedelta(minutes=20))
    data = client.post("/api/v1/heartbeat", json=body(), headers=AUTH).json()
    assert data["commands"] == []
    assert app_store.list_commands(player)[0]["expired_at"] is not None


def test_revoked_player_heartbeat_401(client, player, app_store):
    app_store.revoke_player(player)
    assert client.post("/api/v1/heartbeat", json=body(), headers=AUTH).status_code == 401
