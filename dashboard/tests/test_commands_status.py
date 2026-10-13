from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from coral_dashboard.repo import Conflict
from dash_helpers import add_media

NOW = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)


@pytest.fixture
def player(store):
    playlist = store.create_playlist("Loja")
    store.add_item(playlist, add_media(store, 1))
    return store.create_player("pombal", "a" * 64, playlist_id=playlist)


def test_closed_command_list(store, player):
    store.queue_command(player, "sync-now", NOW)
    with pytest.raises(ValueError):
        store.queue_command(player, "rm -rf /", NOW)
    with pytest.raises(sqlite3.IntegrityError):  # the database CHECK also refuses it
        store.conn.execute(
            "INSERT INTO player_commands (player_id, name, created_at) VALUES (?, 'shell', ?)",
            (player, NOW.isoformat()),
        )


def test_revoked_player_gets_no_commands(store, player):
    store.revoke_player(player)
    with pytest.raises(Conflict):
        store.queue_command(player, "reboot", NOW)


def test_deliver_until_acked(store, player):
    first = store.queue_command(player, "sync-now", NOW)
    second = store.queue_command(player, "reboot", NOW)
    assert [r["id"] for r in store.deliver_commands(player, NOW)] == [first, second]
    # Delivered but not acked: sent again (the player ignores ids it already ran).
    assert [r["id"] for r in store.deliver_commands(player, NOW + timedelta(minutes=30))] == [
        first,
        second,
    ]
    store.ack_commands(player, [first], NOW)
    assert [r["id"] for r in store.deliver_commands(player, NOW)] == [second]
    store.ack_commands(player, [second, 999], NOW)  # unknown ids are ignored
    assert store.deliver_commands(player, NOW) == []


def test_undelivered_command_expires_after_15_min(store, player):
    old = store.queue_command(player, "reboot", NOW)
    fresh = store.queue_command(player, "sync-now", NOW + timedelta(minutes=10))
    later = NOW + timedelta(minutes=16)
    assert [r["id"] for r in store.deliver_commands(player, later)] == [fresh]
    rows = {r["id"]: r for r in store.list_commands(player)}
    assert rows[old]["expired_at"] is not None
    assert rows[old]["delivered_at"] is None
    # Expired commands never come back.
    assert old not in [r["id"] for r in store.deliver_commands(player, later)]


def test_commands_are_per_player(store, player):
    other = store.create_player("coimbra", "b" * 64)
    store.queue_command(other, "reboot", NOW)
    assert store.deliver_commands(player, NOW) == []
    store.ack_commands(player, [1], NOW)  # cannot ack another player's command
    assert store.list_commands(other)[0]["acked_at"] is None


def test_online_offline_from_last_seen(store, player):
    row = store.get_player(player)
    status = store.player_status(row, NOW, 60)
    assert (status.online, status.seen_ago_s, status.state) == (False, None, {})

    store.record_heartbeat(player, {"playlist_hash_seen": "x"}, NOW)
    row = store.get_player(player)
    assert store.player_status(row, NOW + timedelta(seconds=20), 60).online
    assert store.player_status(row, NOW + timedelta(seconds=180), 60).online
    late = store.player_status(row, NOW + timedelta(seconds=181), 60)
    assert (late.online, late.seen_ago_s) == (False, 181)


def test_in_sync_compares_seen_hash_with_desired(store, player):
    row = store.get_player(player)
    desired = store.player_etag(row)
    store.record_heartbeat(player, {"playlist_hash_seen": desired}, NOW)
    assert store.player_status(store.get_player(player), NOW, 60).in_sync
    store.record_heartbeat(player, {"playlist_hash_seen": "old"}, NOW)
    assert not store.player_status(store.get_player(player), NOW, 60).in_sync
