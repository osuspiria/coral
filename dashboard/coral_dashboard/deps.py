"""Shared FastAPI dependencies: database store, admin session, CSRF, player token."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request

from .auth import SESSION_COOKIE, Session, SessionSigner, hash_token
from .config import Settings
from .db import connect
from .manifest import ManifestContext
from .repo import Store

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


class LoginRequired(Exception):
    """Raised for unauthenticated admin requests; handled as a redirect or 401."""


@dataclass
class AppState:
    settings: Settings
    ctx: ManifestContext
    sessions: SessionSigner
    login_sessions: SessionSigner


def app_state(request: Request) -> AppState:
    return request.app.state.coral


def get_store(state: AppState = Depends(app_state)) -> Iterator[Store]:
    conn = connect(state.settings.db_path)
    try:
        yield Store(conn, state.ctx)
    finally:
        conn.close()


async def require_admin(request: Request, state: AppState = Depends(app_state)) -> Session:
    """Valid admin session; for unsafe methods also a valid CSRF token.

    The token comes from the X-CSRF-Token header (fetch requests, including the
    streaming upload whose body must not be read here) or the csrf_token form field.
    """
    session = state.sessions.load(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise LoginRequired()
    if request.method in UNSAFE_METHODS:
        token = request.headers.get("x-csrf-token")
        content_type = request.headers.get("content-type", "")
        if token is None and content_type.startswith(
            ("application/x-www-form-urlencoded", "multipart/form-data")
        ):
            token = (await request.form()).get("csrf_token")
        if not state.sessions.check_csrf(session, token if isinstance(token, str) else None):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")
    return session


def require_player(request: Request, store: Store = Depends(get_store)) -> sqlite3.Row:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(401, "Missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    player = store.get_active_player_by_token_hash(hash_token(token.strip()))
    if player is None:
        raise HTTPException(401, "Invalid or revoked token", headers={"WWW-Authenticate": "Bearer"})
    return player
