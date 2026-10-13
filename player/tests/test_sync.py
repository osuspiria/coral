from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest
from coral_player.client import ServerClient, ServerError, Unauthorized
from coral_player.config import Config
from coral_player.manifest import parse_manifest
from coral_player.state import State
from coral_player.sync import CorruptDownload, InsufficientSpace, MediaCache
from fake_server import FakeServer
from player_helpers import blob, item_for, make_manifest


@pytest.fixture
def server():
    with FakeServer() as srv:
        yield srv


@pytest.fixture
def cfg(tmp_path, server):
    return Config(server_url=server.url, data_dir=tmp_path / "var", min_free_mb=0, http_timeout_s=5)


@pytest.fixture
def cache(cfg):
    c = MediaCache(cfg)
    c.prepare()
    return c


def publish(server, *contents):
    items = []
    for pos, content in enumerate(contents, start=1):
        sha = hashlib.sha256(content).hexdigest()
        server.files[sha] = content
        items.append(item_for(content, pos))
    server.manifest = make_manifest(items)
    return parse_manifest(server.manifest)


def client_for(server, token="tok"):
    return ServerClient(server.url, token, 5)


def test_downloads_verifies_and_promotes(server, cache):
    manifest = publish(server, blob(1), blob(2, 300_000))
    assert cache.ensure(manifest.items, client_for(server)) == 2
    for item in manifest.items:
        assert cache.path_for(item.filename).read_bytes() == server.files[item.sha256]
    assert list(cache.staging_dir.iterdir()) == []
    # Second run downloads nothing.
    server.requests.clear()
    assert cache.ensure(manifest.items, client_for(server)) == 0
    assert server.requests == []


def test_corrupted_file_is_not_promoted(server, cache):
    """Spec §9: corrupted/incomplete file fails sha256 and is never promoted."""
    manifest = publish(server, blob(1))
    server.corrupt.add(manifest.items[0].sha256)
    with pytest.raises(CorruptDownload, match="sha256"):
        cache.ensure(manifest.items, client_for(server))
    assert not cache.path_for(manifest.items[0].filename).exists()
    assert list(cache.staging_dir.iterdir()) == []


def test_interrupted_download_resumes_with_range(server, cache):
    content = blob(7, 500_000)
    manifest = publish(server, content)
    sha = manifest.items[0].sha256
    server.cut_after[sha] = 200_000
    with pytest.raises(ServerError, match="interrupted"):
        cache.ensure(manifest.items, client_for(server))
    part = cache.staging_dir / f"{sha}.part"
    assert 0 < part.stat().st_size < len(content)
    assert not cache.path_for(manifest.items[0].filename).exists()

    have = part.stat().st_size
    cache.ensure(manifest.items, client_for(server))
    assert cache.path_for(manifest.items[0].filename).read_bytes() == content
    media_requests = [h for p, h in server.requests if p.startswith("/api/v1/media/")]
    assert media_requests[-1]["Range"] == f"bytes={have}-"


def test_wrong_size_file_in_media_is_replaced(server, cache):
    manifest = publish(server, blob(1))
    target = cache.path_for(manifest.items[0].filename)
    target.write_bytes(b"truncated")
    cache.ensure(manifest.items, client_for(server))
    assert target.read_bytes() == blob(1)


def test_disk_full_refuses_download(server, cfg, tmp_path):
    """Spec §9: disk nearly full -> sync refuses to download."""
    cache = MediaCache(Config(server_url=server.url, data_dir=tmp_path / "v2", min_free_mb=10**9))
    cache.prepare()
    manifest = publish(server, blob(1))
    with pytest.raises(InsufficientSpace):
        cache.ensure(manifest.items, client_for(server))
    assert server.paths() == []


def test_revoked_token(server, cache):
    manifest = publish(server, blob(1))
    with pytest.raises(Unauthorized):
        cache.ensure(manifest.items, client_for(server, "revoked"))
    with pytest.raises(Unauthorized):
        client_for(server, "revoked").fetch_manifest(None)


def test_fetch_manifest_etag(server):
    publish(server, blob(1))
    client = client_for(server)
    first = client.fetch_manifest(None)
    assert first.changed and first.etag == server.manifest["playlist_hash"]
    assert first.server_time is not None
    second = client.fetch_manifest(first.etag)
    assert not second.changed and second.raw is None


@pytest.mark.parametrize("status", [404, 500, 503])
def test_fetch_manifest_errors(server, status):
    publish(server, blob(1))
    server.manifest_status = status
    with pytest.raises(ServerError) as exc:
        client_for(server).fetch_manifest(None)
    assert exc.value.status == status


def test_network_down():
    client = ServerClient("http://127.0.0.1:9", "tok", 1)
    with pytest.raises(ServerError, match="network"):
        client.fetch_manifest(None)


def test_cleanup_respects_grace_period_and_limit(server, cfg, cache):
    names = []
    for n in range(4):
        content = blob(n)
        name = f"{hashlib.sha256(content).hexdigest()}.png"
        cache.path_for(name).write_bytes(content)
        names.append(name)
    (cache.media_dir / "notes.txt").write_text("not ours")
    state = State()
    now = datetime(2026, 10, 13, tzinfo=UTC)
    referenced = {names[0]}

    assert cache.cleanup(referenced, state, now) == []
    assert set(state.orphaned_since) == set(names[1:])

    later = now + timedelta(days=8)
    limited = MediaCache(
        Config(server_url=server.url, data_dir=cfg.data_dir, max_deletions_per_run=2)
    )
    removed = limited.cleanup(referenced, state, later)
    assert len(removed) == 2
    assert len(limited.cleanup(referenced, state, later)) == 1
    assert cache.path_for(names[0]).exists()
    assert (cache.media_dir / "notes.txt").exists()
    assert state.orphaned_since == {}


def test_cleanup_forgets_orphan_when_referenced_again(cache):
    name = f"{'a' * 64}.png"
    cache.path_for(name).write_bytes(b"x")
    state = State()
    now = datetime(2026, 10, 13, tzinfo=UTC)
    cache.cleanup(set(), state, now)
    assert name in state.orphaned_since
    cache.cleanup({name}, state, now + timedelta(days=30))
    assert state.orphaned_since == {}
    assert cache.path_for(name).exists()
