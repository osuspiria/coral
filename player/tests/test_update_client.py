from __future__ import annotations

import json

import pytest
from coral_player.update import UpdateClient


@pytest.fixture
def client(tmp_path):
    (tmp_path / "updater").mkdir()
    return UpdateClient(tmp_path / "var", "0.3.0", tmp_path / "updater")


def set_status(client, **status):
    (client.updater_state / "status.json").write_text(json.dumps(status))


def request(client):
    path = client.dir / "request.json"
    return json.loads(path.read_text()) if path.exists() else None


def test_want_writes_and_withdraws_requests(client):
    client.want("0.4.0")
    assert request(client) == {"version": "0.4.0"}
    client.want("0.3.0")  # already running
    assert request(client) is None
    client.want("0.4.0")
    client.want(None)  # the server no longer offers an update
    assert request(client) is None
    client.want("../evil")
    assert request(client) is None


def test_want_does_not_insist_on_a_failed_version(client):
    set_status(client, state="rolled_back", version="0.4.0")
    client.want("0.4.0")
    assert request(client) is None
    client.want("0.4.1")
    assert request(client) == {"version": "0.4.1"}


def test_report_reflects_updater_status(client):
    assert client.report() == {"state": "idle", "version": None, "last_error": None}
    set_status(client, state="rejected", version="0.4.0", last_error="invalid signature")
    assert client.report() == {
        "state": "rejected",
        "version": "0.4.0",
        "last_error": "invalid signature",
    }
    set_status(client, state="committed", version="0.3.0", last_error=None)
    assert client.report()["state"] == "idle"


def test_commit_only_after_healthy_start(client):
    set_status(client, state="pending_commit", version="0.3.0", nonce="n1")
    assert not client.maybe_commit(heartbeat_accepted=True, playing=False)
    assert not client.maybe_commit(heartbeat_accepted=False, playing=True)
    assert client.maybe_commit(heartbeat_accepted=True, playing=True)
    commit = json.loads((client.dir / "commit.json").read_text())
    assert commit == {"version": "0.3.0", "nonce": "n1"}
    assert not client.maybe_commit(heartbeat_accepted=True, playing=True)  # once


def test_no_commit_for_another_version_or_without_pending(client):
    set_status(client, state="pending_commit", version="0.4.0", nonce="n1")
    assert not client.maybe_commit(heartbeat_accepted=True, playing=True)
    set_status(client, state="committed", version="0.3.0")
    assert not client.maybe_commit(heartbeat_accepted=True, playing=True)
