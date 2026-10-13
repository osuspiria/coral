from __future__ import annotations

import json

import pytest
from coral_player.config import ConfigError, load_config
from coral_player.fsutil import atomic_write_json, read_json
from coral_player.state import PlayEntry, State, StateStore


def write(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_config(tmp_path):
    cfg = load_config(write(tmp_path, 'server_url = "https://example.com/"\ndata_dir = "/x"\n'))
    assert cfg.server_url == "https://example.com"
    assert cfg.media_dir.as_posix() == "/x/media"
    assert cfg.poll_interval_s == 60


@pytest.mark.parametrize(
    "text",
    [
        'server_url = "http://example.com"',
        'server_url = "ftp://example.com"',
        "data_dir = '/x'",
        'server_url = "https://example.com"\nunknown_key = 1',
        "not toml at all [",
    ],
)
def test_bad_config(tmp_path, text):
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, text))


def test_http_allowed_only_for_localhost(tmp_path):
    assert load_config(write(tmp_path, 'server_url = "http://127.0.0.1:8000"')).server_url


def test_token_file(tmp_path):
    cfg = load_config(write(tmp_path, 'server_url = "https://e.com"\ntoken_file = "missing"'))
    with pytest.raises(ConfigError):
        cfg.read_token()


def test_atomic_write_leaves_no_temp(tmp_path):
    target = tmp_path / "x.json"
    atomic_write_json(target, {"a": 1})
    atomic_write_json(target, {"a": 2})
    assert read_json(target) == {"a": 2}
    assert [p.name for p in tmp_path.iterdir()] == ["x.json"]


def test_read_json_tolerates_corruption(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b'{"half": ')
    assert read_json(path) is None
    assert read_json(tmp_path / "missing.json") is None


def test_state_round_trip_and_corrupt_state(tmp_path):
    store = StateStore(tmp_path / "state.json")
    state = State(
        playing=[PlayEntry("a" * 64, "png", "image", 10)],
        cleared=True,
        orphaned_since={"b" * 64: "2026-10-01T00:00:00+00:00"},
    )
    store.save(state)
    assert store.load() == state

    (tmp_path / "state.json").write_text(json.dumps({"playing": [{"bad": 1}]}))
    assert store.load().playing == []
    (tmp_path / "state.json").write_text("garbage")
    assert store.load() == State()
