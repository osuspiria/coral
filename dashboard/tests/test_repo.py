from __future__ import annotations

import pytest
from coral_dashboard.db import connect, migrate
from coral_dashboard.manifest import build_manifest, manifest_hash
from coral_dashboard.repo import Conflict, NotFound
from dash_helpers import CTX, add_media


def test_migrate_is_idempotent(tmp_path):
    conn = connect(tmp_path / "db.sqlite3")
    assert migrate(conn) == 1
    assert migrate(conn) == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_items_are_ordered_and_reordered(store):
    pl = store.create_playlist("Loja")
    ids = [store.add_item(pl, add_media(store, n)) for n in (1, 2, 3)]
    assert [r["position"] for r in store.list_items(pl)] == [1, 2, 3]

    store.move_item(ids[2], 1)
    assert [r["id"] for r in store.list_items(pl)] == [ids[2], ids[0], ids[1]]

    store.move_item(ids[2], 99)  # clamped to the end
    assert [r["id"] for r in store.list_items(pl)] == [ids[0], ids[1], ids[2]]

    store.remove_item(ids[0])
    items = store.list_items(pl)
    assert [r["id"] for r in items] == [ids[1], ids[2]]
    assert [r["position"] for r in items] == [1, 2]


def test_hash_changes_on_every_relevant_mutation(store):
    pl = store.create_playlist("Loja")
    hashes = [store.get_playlist(pl)["playlist_hash"]]
    item = store.add_item(pl, add_media(store, 1))
    hashes.append(store.get_playlist(pl)["playlist_hash"])
    store.add_item(pl, add_media(store, 2))
    hashes.append(store.get_playlist(pl)["playlist_hash"])
    store.update_item(item, "2026-01-01", "2026-12-31", 5)
    hashes.append(store.get_playlist(pl)["playlist_hash"])
    store.move_item(item, 2)
    hashes.append(store.get_playlist(pl)["playlist_hash"])
    store.remove_item(item)
    hashes.append(store.get_playlist(pl)["playlist_hash"])
    assert len(set(hashes)) == len(hashes)

    store.rename_playlist(pl, "Other name")  # name is not part of the manifest
    assert store.get_playlist(pl)["playlist_hash"] == hashes[-1]


def test_stored_hash_matches_built_manifest(store):
    pl = store.create_playlist("Loja")
    store.add_item(pl, add_media(store, 1), image_duration_s=7)
    store.add_item(pl, add_media(store, 2, kind="video"))
    player = store.get_player(store.create_player("pombal", "t" * 64, playlist_id=pl))
    manifest = store.player_manifest(player)
    assert manifest["playlist_hash"] == store.player_etag(player)
    assert manifest["playlist_hash"] == manifest_hash(manifest)
    assert [i["duration"] for i in manifest["items"]] == [7, 12.5]
    assert manifest["items"][0]["url"] == f"/api/v1/media/{1:064x}"
    assert manifest["items"][0]["ext"] == "png"


def test_default_image_duration_applies(store):
    pl = store.create_playlist("Loja")
    store.add_item(pl, add_media(store, 1))
    manifest = build_manifest(store.list_items(pl), CTX, assigned=True, cleared=False)
    assert manifest["items"][0]["duration"] == 10


@pytest.mark.parametrize(
    ("start", "end"),
    [("2026-13-01", None), ("2026-1-1", None), (None, "tomorrow"), ("2026-02-01", "2026-01-31")],
)
def test_invalid_dates_are_rejected(store, start, end):
    pl = store.create_playlist("Loja")
    with pytest.raises(ValueError):
        store.add_item(pl, add_media(store, 1), start, end)


def test_invalid_image_duration_rejected(store):
    pl = store.create_playlist("Loja")
    with pytest.raises(ValueError):
        store.add_item(pl, add_media(store, 1), image_duration_s=0)


def test_assign_unassign_and_clear(store):
    pl = store.create_playlist("Loja")
    store.add_item(pl, add_media(store, 1))
    pid = store.create_player("pombal", "a" * 64)

    m = store.player_manifest(store.get_player(pid))
    assert (m["assigned"], m["cleared"], m["items"]) == (False, False, [])

    store.assign_playlist(pid, pl)
    m = store.player_manifest(store.get_player(pid))
    assert (m["assigned"], m["cleared"], len(m["items"])) == (True, False, 1)

    store.set_cleared(pid, True)
    m = store.player_manifest(store.get_player(pid))
    assert (m["assigned"], m["cleared"], m["items"]) == (True, True, [])

    store.assign_playlist(pid, pl)  # re-assigning ends the clear
    assert store.get_player(pid)["cleared"] == 0

    store.unassign_playlist(pid, clear=False)
    m = store.player_manifest(store.get_player(pid))
    assert (m["assigned"], m["cleared"]) == (False, False)

    store.assign_playlist(pid, pl)
    store.unassign_playlist(pid, clear=True)
    m = store.player_manifest(store.get_player(pid))
    assert (m["assigned"], m["cleared"], m["items"]) == (False, True, [])


def test_etags_differ_between_states(store):
    pl = store.create_playlist("Loja")
    pid = store.create_player("pombal", "a" * 64)
    unassigned = store.player_etag(store.get_player(pid))
    store.assign_playlist(pid, pl)
    empty_assigned = store.player_etag(store.get_player(pid))
    store.set_cleared(pid, True)
    cleared = store.player_etag(store.get_player(pid))
    assert len({unassigned, empty_assigned, cleared}) == 3


def test_cannot_delete_assigned_playlist(store):
    pl = store.create_playlist("Loja")
    store.create_player("pombal", "a" * 64, playlist_id=pl)
    with pytest.raises(Conflict):
        store.delete_playlist(pl)


def test_delete_playlist_detaches_revoked_players(store):
    pl = store.create_playlist("Loja")
    pid = store.create_player("pombal", "a" * 64, playlist_id=pl)
    store.revoke_player(pid)
    store.delete_playlist(pl)
    with pytest.raises(NotFound):
        store.get_playlist(pl)


def test_duplicate_player_label(store):
    store.create_player("pombal", "a" * 64)
    with pytest.raises(Conflict):
        store.create_player("pombal", "b" * 64)


def test_revoked_player_not_found_by_token(store):
    pid = store.create_player("pombal", "a" * 64)
    assert store.get_active_player_by_token_hash("a" * 64)["id"] == pid
    store.revoke_player(pid)
    assert store.get_active_player_by_token_hash("a" * 64) is None


def test_media_soft_delete_and_collection(store, conn):
    pl = store.create_playlist("Loja")
    used = add_media(store, 1)
    unused = add_media(store, 2)
    store.add_item(pl, used)
    store.delete_media(used)
    store.delete_media(unused)
    assert store.list_media() == []
    # Within the grace period nothing is collected.
    assert store.media_to_collect(grace_days=7) == []
    # Past the grace period only media no playlist references is collected.
    conn.execute("UPDATE media SET deleted_at = '2000-01-01T00:00:00+00:00'")
    assert [r["id"] for r in store.media_to_collect(grace_days=7)] == [unused]
    # The playlist still serves the deleted-but-used media.
    assert len(store.list_items(pl)) == 1
    # Re-uploading revives a soft-deleted row.
    assert add_media(store, 2) == unused
    assert store.get_media(unused)["deleted_at"] is None


def test_cannot_add_deleted_media(store):
    pl = store.create_playlist("Loja")
    media = add_media(store, 1)
    store.delete_media(media)
    with pytest.raises(Conflict):
        store.add_item(pl, media)


def test_player_may_download_only_own_media(store):
    pl = store.create_playlist("Loja")
    store.add_item(pl, add_media(store, 1))
    add_media(store, 2)
    player = store.get_player(store.create_player("pombal", "a" * 64, playlist_id=pl))
    assert store.player_may_download(player, f"{1:064x}")
    assert not store.player_may_download(player, f"{2:064x}")
