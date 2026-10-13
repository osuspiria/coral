from __future__ import annotations

import hashlib

import pytest
from coral_dashboard.auth import hash_token


@pytest.fixture
def setup(app_store, settings):
    """A player with a playlist holding one real file on disk."""
    content = bytes(range(256)) * 40
    sha = hashlib.sha256(content).hexdigest()
    (settings.media_dir / f"{sha}.png").write_bytes(content)
    media_id = app_store.add_media(
        sha256=sha,
        stored_name=f"{sha}.png",
        original_name="a.png",
        kind="image",
        size=len(content),
        duration_s=None,
        codec="png",
        width=10,
        height=10,
    )
    playlist = app_store.create_playlist("Loja")
    app_store.add_item(playlist, media_id)
    player = app_store.create_player("pombal", hash_token("tok-pombal"), playlist_id=playlist)
    return {"sha": sha, "content": content, "playlist": playlist, "player": player}


AUTH = {"Authorization": "Bearer tok-pombal"}


def test_manifest_requires_valid_token(client, setup):
    assert client.get("/api/v1/manifest").status_code == 401
    bad = client.get("/api/v1/manifest", headers={"Authorization": "Bearer nope"})
    assert bad.status_code == 401
    assert bad.headers["www-authenticate"] == "Bearer"


def test_revoked_token_gets_401(client, setup, app_store):
    app_store.revoke_player(setup["player"])
    assert client.get("/api/v1/manifest", headers=AUTH).status_code == 401


def test_manifest_etag_and_304(client, setup, app_store):
    first = client.get("/api/v1/manifest", headers=AUTH)
    assert first.status_code == 200
    body = first.json()
    assert body["assigned"] is True and body["cleared"] is False
    assert body["items"][0]["sha256"] == setup["sha"]
    etag = first.headers["etag"]
    assert etag == f'"{body["playlist_hash"]}"'

    same = client.get("/api/v1/manifest", headers={**AUTH, "If-None-Match": etag})
    assert same.status_code == 304
    assert same.content == b""

    app_store.add_item(setup["playlist"], app_store.get_media_by_sha(setup["sha"])["id"])
    changed = client.get("/api/v1/manifest", headers={**AUTH, "If-None-Match": etag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != etag
    assert len(changed.json()["items"]) == 2


def test_manifest_cleared_and_unassigned(client, setup, app_store):
    app_store.set_cleared(setup["player"], True)
    body = client.get("/api/v1/manifest", headers=AUTH).json()
    assert (body["assigned"], body["cleared"], body["items"]) == (True, True, [])

    app_store.unassign_playlist(setup["player"], clear=False)
    response = client.get("/api/v1/manifest", headers=AUTH)
    assert response.status_code == 200  # never 404 for "no assignment"
    assert (response.json()["assigned"], response.json()["cleared"]) == (False, False)


def test_media_download_and_range(client, setup):
    url = f"/api/v1/media/{setup['sha']}"
    assert client.get(url).status_code == 401
    full = client.get(url, headers=AUTH)
    assert full.status_code == 200
    assert full.content == setup["content"]
    assert full.headers["content-type"] == "image/png"

    partial = client.get(url, headers={**AUTH, "Range": "bytes=1000-"})
    assert partial.status_code == 206
    assert partial.content == setup["content"][1000:]
    assert partial.headers["content-range"] == f"bytes 1000-{len(setup['content']) - 1}/10240"


def test_media_only_for_own_playlist(client, setup, app_store):
    assert client.get("/api/v1/media/" + "0" * 64, headers=AUTH).status_code == 404
    assert client.get("/api/v1/media/..%2F..%2Fetc", headers=AUTH).status_code == 404
    app_store.set_cleared(setup["player"], True)
    assert client.get(f"/api/v1/media/{setup['sha']}", headers=AUTH).status_code == 404
