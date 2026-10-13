from __future__ import annotations

import pytest
from coral_dashboard.auth import hash_token
from dash_helpers import add_media, login

AUTH = {"Authorization": "Bearer tok"}
WARNING = "This won&#39;t clear the screens. Use Clear screen if that&#39;s what you want."


@pytest.fixture
def admin(client):
    csrf = login(client)

    def post(path, **data):
        return client.post(path, data={"csrf_token": csrf, **data})

    return post


def manifest(client):
    return client.get("/api/v1/manifest", headers=AUTH).json()


def test_pages_render(client, admin, app_store):
    pl = app_store.create_playlist("Loja")
    app_store.add_item(pl, add_media(app_store, 1))
    app_store.create_player("pombal", hash_token("tok"), playlist_id=pl)
    for path in ("/players", "/playlists", f"/playlists/{pl}", "/media"):
        response = client.get(path)
        assert response.status_code == 200, path
    assert "pombal" in client.get("/players").text


def test_playlist_crud_flow(client, admin, app_store):
    response = admin("/playlists", name="Loja")
    assert response.status_code == 200
    pl = app_store.list_playlists()[0]["id"]
    m1, m2 = add_media(app_store, 1), add_media(app_store, 2)

    admin(f"/playlists/{pl}/items", media_id=str(m1), image_duration_s="5")
    admin(
        f"/playlists/{pl}/items", media_id=str(m2), start_date="2026-10-01", end_date="2026-10-31"
    )
    items = app_store.list_items(pl)
    assert [(i["media_id"], i["image_duration_s"]) for i in items] == [(m1, 5), (m2, None)]
    assert items[1]["start_date"] == "2026-10-01"

    admin(f"/items/{items[1]['id']}/move", position="1")
    assert [i["media_id"] for i in app_store.list_items(pl)] == [m2, m1]

    admin(
        f"/items/{items[0]['id']}/update", start_date="", end_date="2026-12-24", image_duration_s=""
    )
    updated = app_store.get_item(items[0]["id"])
    assert (updated["end_date"], updated["image_duration_s"]) == ("2026-12-24", None)

    bad = admin(f"/items/{items[0]['id']}/update", start_date="2026-12-31", end_date="2026-01-01")
    assert "must not be after" in bad.text

    admin(f"/playlists/{pl}/rename", name="Shop")
    assert app_store.get_playlist(pl)["name"] == "Shop"

    admin(f"/playlists/{pl}/delete")
    assert app_store.list_playlists() == []


def test_empty_assigned_playlist_warns(client, admin, app_store):
    pl = app_store.create_playlist("Loja")
    item = app_store.add_item(pl, add_media(app_store, 1))
    app_store.create_player("pombal", hash_token("tok"), playlist_id=pl)
    response = admin(f"/items/{item}/remove")
    assert WARNING in response.text
    assert WARNING in client.get(f"/playlists/{pl}").text
    # The manifest really is empty and not cleared: the player will refuse it.
    body = manifest(client)
    assert (body["items"], body["cleared"]) == ([], False)


def test_cannot_delete_assigned_playlist_via_web(client, admin, app_store):
    pl = app_store.create_playlist("Loja")
    app_store.create_player("pombal", hash_token("tok"), playlist_id=pl)
    response = admin(f"/playlists/{pl}/delete")
    assert "Unassign it first" in response.text


def test_assign_clear_resume_unassign(client, admin, app_store):
    pl = app_store.create_playlist("Loja")
    app_store.add_item(pl, add_media(app_store, 1))
    pid = app_store.create_player("pombal", hash_token("tok"))
    assert manifest(client)["assigned"] is False

    admin(f"/players/{pid}/assign", playlist_id=str(pl))
    assert len(manifest(client)["items"]) == 1

    admin(f"/players/{pid}/clear")
    body = manifest(client)
    assert (body["cleared"], body["items"]) == (True, [])

    admin(f"/players/{pid}/resume")
    assert len(manifest(client)["items"]) == 1

    # Unassign without clearing: player keeps its last list (not a clear).
    admin(f"/players/{pid}/unassign", clear="no")
    body = manifest(client)
    assert (body["assigned"], body["cleared"]) == (False, False)

    # Unassign with clearing: explicit clear.
    admin(f"/players/{pid}/assign", playlist_id=str(pl))
    admin(f"/players/{pid}/unassign", clear="yes")
    body = manifest(client)
    assert (body["assigned"], body["cleared"]) == (False, True)

    # Assigning again ends the clear.
    admin(f"/players/{pid}/assign", playlist_id=str(pl))
    body = manifest(client)
    assert (body["assigned"], body["cleared"], len(body["items"])) == (True, False, 1)


def test_unassign_requires_explicit_choice(client, admin, app_store):
    pl = app_store.create_playlist("Loja")
    pid = app_store.create_player("pombal", hash_token("tok"), playlist_id=pl)
    response = admin(f"/players/{pid}/unassign", clear="maybe")
    assert "Choose whether to clear" in response.text
    assert app_store.get_player(pid)["playlist_id"] == pl


def test_unknown_ids_are_404(client, admin):
    assert client.get("/playlists/999").status_code == 404
    assert admin("/players/999/clear").status_code == 404


def test_player_page_status_and_commands(client, admin, app_store, app):
    from datetime import UTC, datetime

    now = datetime(2026, 10, 14, 10, 0, tzinfo=UTC)
    app.state.coral.clock = lambda: now
    pl = app_store.create_playlist("Loja")
    app_store.add_item(pl, add_media(app_store, 1))
    pid = app_store.create_player("pombal", hash_token("tok"), playlist_id=pl)

    page = client.get(f"/players/{pid}").text
    assert "No heartbeat received yet" in page
    assert "offline" in client.get("/players").text

    state = {
        "agent_version": "0.2.0",
        "player_time": "x",
        "playlist_hash_applied": None,
        "playlist_hash_seen": app_store.player_etag(app_store.get_player(pid)),
        "cleared": False,
        "clock_drift": True,
        "mpv": {"state": "playing", "current_file": "abc.png", "progressing": True, "restarts": 2},
        "sync": {"last_ok": None, "last_error": "network error"},
        "update": {"state": "idle", "last_error": None},
        "system": {"disk_free_mb": 2048, "temp_c": 48.5, "uptime_s": 3600},
        "acked_commands": [],
    }
    app_store.record_heartbeat(pid, state, now)
    listing = client.get("/players").text
    assert "online" in listing and "0.2.0" in listing and "clock drift" in listing
    detail = client.get(f"/players/{pid}").text
    assert "in sync" in detail and "2 mpv restart(s)" in detail and "network error" in detail

    response = admin(f"/players/{pid}/commands", name="sync-now")
    assert "sync-now queued" in response.text
    assert "pending" in client.get(f"/players/{pid}").text
    bad = admin(f"/players/{pid}/commands", name="shutdown")
    assert "Unknown command" in bad.text
    assert [c["name"] for c in app_store.list_commands(pid)] == ["sync-now"]


def test_commands_require_csrf(client, app_store):
    login(client)
    pid = app_store.create_player("pombal", hash_token("tok"))
    assert client.post(f"/players/{pid}/commands", data={"name": "reboot"}).status_code == 403
    assert app_store.list_commands(pid) == []


def test_releases_page_and_player_update_controls(client, admin, app_store):
    app_store.add_release("0.3.0", "a" * 64, 2048, "sig")
    app_store.add_release("0.3.1", "a" * 64, 2048, "sig")
    pid = app_store.create_player("pombal", hash_token("tok"))
    page = client.get("/releases").text
    assert "0.3.0" in page and "canary" in page and "stable" in page

    assert "Channel stable set to 0.3.0" in admin("/releases/channels/stable", version="0.3.0").text
    assert app_store.effective_desired_version(app_store.get_player(pid)) == "0.3.0"
    assert "Unknown channel" in admin("/releases/channels/beta", version="0.3.0").text

    admin(f"/players/{pid}/channel", channel="canary")
    admin("/releases/channels/canary", version="0.3.1")
    assert app_store.effective_desired_version(app_store.get_player(pid)) == "0.3.1"

    admin(f"/players/{pid}/version", version="0.3.0")
    assert app_store.get_player(pid)["desired_version"] == "0.3.0"
    detail = client.get(f"/players/{pid}").text
    assert "player override" in detail

    app_store.record_update_report(pid, {"state": "rolled_back", "version": "0.3.0"})
    detail = client.get(f"/players/{pid}").text
    assert "not offered: failed on this player" in detail and "Try again" in detail
    admin(f"/players/{pid}/clear-failed")
    assert app_store.get_player(pid)["failed_version"] is None

    admin(f"/players/{pid}/version", version="")
    assert app_store.get_player(pid)["desired_version"] is None
