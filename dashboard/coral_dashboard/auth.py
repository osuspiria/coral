"""Admin authentication: argon2id password, HMAC-signed session cookie, CSRF tokens."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

SESSION_COOKIE = "coral_session"
LOGIN_COOKIE = "coral_login"

_hasher = PasswordHasher()  # argon2id with the library's current defaults


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def hash_token(token: str) -> str:
    """Player tokens are 256-bit random values, so a plain sha256 is enough."""
    return hashlib.sha256(token.encode()).hexdigest()


def new_player_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Session:
    nonce: str
    expires_at: int


class SessionSigner:
    def __init__(self, secret_key: str, lifetime_s: int, purpose: str = "session") -> None:
        self._key = hashlib.sha256(f"coral-{purpose}:{secret_key}".encode()).digest()
        self._lifetime_s = lifetime_s

    def _sign(self, payload: bytes) -> str:
        return base64.urlsafe_b64encode(hmac.digest(self._key, payload, "sha256")).decode()

    def issue(self, now: float | None = None) -> tuple[str, Session]:
        now = time.time() if now is None else now
        session = Session(nonce=secrets.token_urlsafe(16), expires_at=int(now) + self._lifetime_s)
        payload = base64.urlsafe_b64encode(
            json.dumps({"n": session.nonce, "e": session.expires_at}).encode()
        )
        return f"{payload.decode()}.{self._sign(payload)}", session

    def load(self, cookie: str | None, now: float | None = None) -> Session | None:
        if not cookie or "." not in cookie:
            return None
        payload, signature = cookie.rsplit(".", 1)
        if not hmac.compare_digest(self._sign(payload.encode()), signature):
            return None
        try:
            data = json.loads(base64.urlsafe_b64decode(payload.encode()))
            session = Session(nonce=str(data["n"]), expires_at=int(data["e"]))
        except (ValueError, KeyError, TypeError):
            return None
        if session.expires_at <= (time.time() if now is None else now):
            return None
        return session

    def csrf_token(self, session: Session) -> str:
        return self._sign(("csrf:" + session.nonce).encode())

    def check_csrf(self, session: Session, token: str | None) -> bool:
        return bool(token) and hmac.compare_digest(self.csrf_token(session), token)
