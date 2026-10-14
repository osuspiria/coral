from __future__ import annotations

import re

from coral_dashboard.ratelimit import LOCK_S, LoginThrottle
from dash_helpers import ADMIN_PASSWORD


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_first_failures_are_free_then_delay_doubles():
    clock = Clock()
    throttle = LoginThrottle(clock)
    for _ in range(4):
        throttle.failure("1.2.3.4")
        assert throttle.wait_seconds("1.2.3.4") == 0
    waits = []
    for _ in range(8):
        throttle.failure("1.2.3.4")
        waits.append(throttle.wait_seconds("1.2.3.4"))
    assert waits == [1, 2, 4, 8, 16, 32, 60, 60]
    assert throttle.wait_seconds("5.6.7.8") == 0  # per IP
    clock.t += 61
    assert throttle.wait_seconds("1.2.3.4") == 0


def test_lockout_and_reset():
    clock = Clock()
    throttle = LoginThrottle(clock)
    for _ in range(20):
        throttle.failure("ip")
    assert throttle.wait_seconds("ip") == LOCK_S
    clock.t += LOCK_S
    assert throttle.wait_seconds("ip") == 0
    for _ in range(10):
        throttle.failure("ip")
    throttle.success("ip")
    assert throttle.wait_seconds("ip") == 0


def login_attempt(client, password):
    page = client.get("/login").text
    token = re.search(r'name="csrf_token" value="([^"]*)"', page).group(1)
    return client.post(
        "/login", data={"password": password, "csrf_token": token}, follow_redirects=False
    )


def test_login_is_throttled_even_with_the_right_password(client):
    for _ in range(5):
        login_attempt(client, "wrong")
    refused = login_attempt(client, ADMIN_PASSWORD)
    assert "Too+many+failed+attempts" in refused.headers["location"]
    assert "coral_session" not in refused.headers.get("set-cookie", "")


def test_successful_login_clears_failures(client, app):
    for _ in range(3):
        login_attempt(client, "wrong")
    ok = login_attempt(client, ADMIN_PASSWORD)
    assert ok.headers["location"] == "/"
    assert app.state.coral.login_throttle.wait_seconds("testclient") == 0
