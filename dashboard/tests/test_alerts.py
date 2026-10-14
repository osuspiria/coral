from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from coral_dashboard import alerts
from coral_dashboard.notify import Notifier, ping_healthcheck
from dash_helpers import make_settings

NOW = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


class FakeNtfy:
    def __init__(self):
        self.messages = []
        self.status = 200
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _answer(self):
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode()
                fake.messages.append(
                    {
                        "title": self.headers.get("Title"),
                        "priority": self.headers.get("Priority"),
                        "auth": self.headers.get("Authorization"),
                        "body": body,
                    }
                )
                self.send_response(fake.status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_POST = _answer  # noqa: N815
            do_GET = _answer  # noqa: N815

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/coral-topic"


@pytest.fixture
def ntfy():
    fake = FakeNtfy()
    yield fake
    fake.httpd.shutdown()


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


def state(**over):
    data = {
        "agent_version": "0.3.4",
        "player_time": NOW.isoformat(),
        "cleared": False,
        "clock_drift": False,
        "mpv": {"state": "playing"},
        "sync": {"last_ok": NOW.isoformat(), "last_error": None},
        "update": {"state": "idle"},
        "system": {"disk_free_mb": 100000, "temp_c": 50.0, "uptime_s": 10},
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return data


@pytest.fixture
def player(store):
    return store.create_player("pombal", "t" * 64, location="Loja")


def kinds(store, player):
    return set(store.open_alerts(player))


def cycle(store, settings, notifier, now=NOW):
    alerts.run_cycle(store, settings, notifier, now)


def test_healthy_player_has_no_alerts(store, settings, player):
    store.record_heartbeat(player, state(), NOW)
    cycle(store, settings, Notifier(""))
    assert kinds(store, player) == set()


def test_never_seen_player_is_not_offline(store, settings, player):
    cycle(store, settings, Notifier(""))
    assert kinds(store, player) == set()


def test_offline_opens_and_resolves(store, settings, player):
    store.record_heartbeat(player, state(), NOW)
    cycle(store, settings, Notifier(""), NOW + timedelta(minutes=10))
    assert kinds(store, player) == set()
    cycle(store, settings, Notifier(""), NOW + timedelta(minutes=11))
    assert kinds(store, player) == {"offline"}
    later = NOW + timedelta(minutes=12)
    store.record_heartbeat(player, state(player_time=later.isoformat()), later)
    cycle(store, settings, Notifier(""), later)
    assert kinds(store, player) == set()


OLD_OK = (NOW - timedelta(minutes=31)).isoformat()
SKEWED = (NOW + timedelta(minutes=5)).isoformat()


@pytest.mark.parametrize(
    ("report", "kind"),
    [
        ({"sync": {"last_ok": OLD_OK, "last_error": "x"}}, "sync_failing"),
        ({"sync": {"last_ok": None, "last_error": "network"}}, "sync_failing"),
        ({"update": {"state": "rolled_back", "version": "0.3.3"}}, "update_failed"),
        ({"update": {"state": "rejected", "version": "0.3.3"}}, "update_failed"),
        ({"system": {"disk_free_mb": 1000}}, "disk_low"),
        ({"system": {"temp_c": 85.5}}, "temp_high"),
        ({"clock_drift": True}, "clock_drift"),
        ({"player_time": SKEWED}, "clock_drift"),
    ],
)
def test_state_rules(store, settings, player, report, kind):
    store.record_heartbeat(player, state(**report), NOW)
    cycle(store, settings, Notifier(""))
    assert kinds(store, player) == {kind}


def test_recent_sync_error_is_not_an_alert_yet(store, settings, player):
    recent = (NOW - timedelta(minutes=5)).isoformat()
    store.record_heartbeat(player, state(sync={"last_ok": recent, "last_error": "x"}), NOW)
    cycle(store, settings, Notifier(""))
    assert kinds(store, player) == set()


def test_mpv_needs_two_bad_heartbeats_and_ignores_cleared(store, settings, player):
    store.record_heartbeat(player, state(mpv={"state": "stuck"}), NOW)
    cycle(store, settings, Notifier(""))
    assert kinds(store, player) == set()
    store.record_heartbeat(player, state(mpv={"state": "stuck"}), NOW)
    cycle(store, settings, Notifier(""))
    assert kinds(store, player) == {"mpv_not_playing"}
    store.record_heartbeat(player, state(), NOW)
    cycle(store, settings, Notifier(""))
    assert kinds(store, player) == set()
    for _ in range(3):  # explicitly cleared screens are not "not playing"
        store.record_heartbeat(player, state(mpv={"state": "idle"}, cleared=True), NOW)
    cycle(store, settings, Notifier(""))
    assert kinds(store, player) == set()


def test_no_duplicates_and_threshold_settings(store, tmp_path, player):
    settings = make_settings(tmp_path / "x", alert_disk_min_mb=500)
    store.record_heartbeat(player, state(system={"disk_free_mb": 1000}), NOW)
    cycle(store, settings, Notifier(""))
    assert kinds(store, player) == set()
    store.record_heartbeat(player, state(system={"disk_free_mb": 100}), NOW)
    for _ in range(3):
        cycle(store, settings, Notifier(""))
    assert len(store.list_alerts()) == 1


def test_notifications_open_and_resolve(store, settings, player, ntfy):
    notifier = Notifier(ntfy.url, "secret-token")
    store.record_heartbeat(player, state(system={"disk_free_mb": 10}), NOW)
    cycle(store, settings, notifier)
    assert len(ntfy.messages) == 1
    sent = ntfy.messages[0]
    assert sent["title"] == "pombal (Loja): disk almost full"
    assert sent["priority"] == "high"
    assert sent["auth"] == "Bearer secret-token"
    assert "10 MB free" in sent["body"]
    cycle(store, settings, notifier)
    assert len(ntfy.messages) == 1  # not sent twice
    store.record_heartbeat(player, state(), NOW)
    cycle(store, settings, notifier)
    assert ntfy.messages[-1]["title"].startswith("RESOLVED pombal")
    assert len(ntfy.messages) == 2


def test_failed_notification_is_retried(store, settings, player, ntfy):
    notifier = Notifier(ntfy.url)
    ntfy.status = 500
    store.record_heartbeat(player, state(system={"temp_c": 90}), NOW)
    cycle(store, settings, notifier)
    assert store.list_alerts()[0]["notified_at"] is None
    ntfy.status = 200
    cycle(store, settings, notifier)
    assert store.list_alerts()[0]["notified_at"] is not None


def test_alert_gone_before_notification_is_not_sent(store, settings, player, ntfy):
    ntfy.status = 500
    notifier = Notifier(ntfy.url)
    store.record_heartbeat(player, state(system={"temp_c": 90}), NOW)
    cycle(store, settings, notifier)
    store.record_heartbeat(player, state(), NOW)
    ntfy.status = 200
    before = len(ntfy.messages)
    cycle(store, settings, notifier)
    assert len(ntfy.messages) == before
    assert store.alerts_to_notify() == []


def test_revoked_player_alerts_are_resolved(store, settings, player):
    store.record_heartbeat(player, state(system={"temp_c": 90}), NOW)
    cycle(store, settings, Notifier(""))
    store.revoke_player(player)
    cycle(store, settings, Notifier(""))
    assert kinds(store, player) == set()


def test_healthcheck_ping(ntfy):
    assert ping_healthcheck(ntfy.url)
    assert not ping_healthcheck("")
    assert not ping_healthcheck("http://127.0.0.1:9/never")


def test_broken_state_json_is_tolerated(store, settings, player):
    store.conn.execute(
        "UPDATE players SET last_seen = ?, last_state = 'not json' WHERE id = ?",
        (NOW.isoformat(), player),
    )
    cycle(store, settings, Notifier(""))
    assert kinds(store, player) == set()
