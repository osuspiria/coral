from __future__ import annotations

import pytest
from coral_dashboard.repo import Conflict, NotFound

SHA = "a" * 64


@pytest.fixture
def player(store):
    return store.create_player("pombal", "t" * 64)


def test_add_and_list_releases(store):
    store.add_release("0.3.0", SHA, 1000, "sig")
    store.add_release("0.3.1", SHA, 1000, "sig")
    assert [r["version"] for r in store.list_releases()] == ["0.3.1", "0.3.0"]
    with pytest.raises(Conflict):
        store.add_release("0.3.0", SHA, 1000, "sig")


@pytest.mark.parametrize("version", ["0.3", "v0.3.0", "0.3.0-rc1", "../0.3.0", ""])
def test_invalid_versions(store, version):
    with pytest.raises(ValueError):
        store.add_release(version, SHA, 1000, "sig")


def test_channel_and_override(store, player):
    store.add_release("0.3.0", SHA, 1, "s")
    store.add_release("0.4.0", SHA, 1, "s")
    row = store.get_player(player)
    assert row["channel"] == "stable"
    assert store.effective_desired_version(row) is None

    store.set_channel_version("stable", "0.3.0")
    store.set_channel_version("canary", "0.4.0")
    assert store.effective_desired_version(store.get_player(player)) == "0.3.0"

    store.set_player_channel(player, "canary")
    assert store.effective_desired_version(store.get_player(player)) == "0.4.0"

    store.set_player_version(player, "0.3.0")  # per-player override wins
    assert store.effective_desired_version(store.get_player(player)) == "0.3.0"
    store.set_player_version(player, None)
    assert store.effective_desired_version(store.get_player(player)) == "0.4.0"

    with pytest.raises(NotFound):
        store.set_channel_version("stable", "9.9.9")
    with pytest.raises(ValueError):
        store.set_player_channel(player, "beta")


def test_failed_version_is_not_requested_again(store, player):
    store.add_release("0.3.0", SHA, 1, "s")
    store.add_release("0.3.1", SHA, 1, "s")
    store.set_channel_version("stable", "0.3.1")
    store.record_update_report(player, {"state": "rolled_back", "version": "0.3.1"})
    row = store.get_player(player)
    assert row["failed_version"] == "0.3.1"
    assert store.effective_desired_version(row) is None  # no update/rollback loop

    store.set_channel_version("stable", "0.3.0")  # desired version changed
    assert store.effective_desired_version(store.get_player(player)) == "0.3.0"

    store.set_channel_version("stable", "0.3.1")
    assert store.effective_desired_version(store.get_player(player)) is None
    store.clear_failed_version(player)  # admin asks to try again
    assert store.effective_desired_version(store.get_player(player)) == "0.3.1"


def test_update_report_states(store, player):
    store.record_update_report(player, {"state": "committed", "version": "0.3.0"})
    assert store.get_player(player)["failed_version"] is None
    store.record_update_report(player, {"state": "rejected", "version": "../x"})
    assert store.get_player(player)["failed_version"] is None
    store.record_update_report(player, {"state": "rejected", "version": "0.3.2"})
    assert store.get_player(player)["failed_version"] == "0.3.2"
