from __future__ import annotations

import re

from coral_dashboard.auth import SessionSigner, hash_password, verify_password
from dash_helpers import ADMIN_PASSWORD, login


def test_password_hash_is_argon2id():
    hashed = hash_password("secret")
    assert hashed.startswith("$argon2id$")
    assert verify_password(hashed, "secret")
    assert not verify_password(hashed, "wrong")
    assert not verify_password("garbage", "secret")


def test_session_signing():
    signer = SessionSigner("k" * 40, 60)
    cookie, session = signer.issue(now=1000)
    assert signer.load(cookie, now=1010) == session
    assert signer.load(cookie, now=1061) is None  # expired
    assert signer.load(cookie[:-2] + "xx", now=1010) is None  # tampered
    assert SessionSigner("o" * 40, 60).load(cookie, now=1010) is None  # other key
    assert signer.check_csrf(session, signer.csrf_token(session))
    assert not signer.check_csrf(session, "nope")
    assert not signer.check_csrf(session, None)


def test_pages_require_login(client):
    response = client.get("/players", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_login_flow_and_cookie_flags(client):
    page = client.get("/login")
    login_csrf = re.search(r'name="csrf_token" value="([^"]*)"', page.text).group(1)

    wrong = client.post(
        "/login", data={"password": "nope", "csrf_token": login_csrf}, follow_redirects=False
    )
    assert wrong.headers["location"].startswith("/login?error=")

    ok = client.post(
        "/login",
        data={"password": ADMIN_PASSWORD, "csrf_token": login_csrf},
        follow_redirects=False,
    )
    assert ok.status_code == 303
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie


def test_login_requires_login_csrf(client):
    client.get("/login")
    response = client.post(
        "/login", data={"password": ADMIN_PASSWORD, "csrf_token": "bad"}, follow_redirects=False
    )
    assert "error=" in response.headers["location"]


def test_unsafe_requests_require_csrf(client):
    csrf = login(client)
    assert client.post("/logout", data={}).status_code == 403
    assert client.post("/logout", data={"csrf_token": "bad"}).status_code == 403
    response = client.post("/logout", headers={"X-CSRF-Token": csrf}, follow_redirects=False)
    assert response.status_code == 303


def test_security_headers(client):
    response = client.get("/login")
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
