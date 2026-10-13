"""Admin web interface (server-rendered, vanilla HTML/CSS/JS)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import media
from .auth import LOGIN_COOKIE, SESSION_COOKIE, Session, verify_password
from .deps import AppState, app_state, get_store, require_admin
from .repo import Store

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
router = APIRouter()


def render(
    request: Request,
    name: str,
    session: Session | None,
    status_code: int = 200,
    **context: Any,
) -> HTMLResponse:
    state: AppState = request.app.state.coral
    csrf = state.sessions.csrf_token(session) if session else ""
    context.setdefault("notice", request.query_params.get("notice"))
    context.setdefault("error", request.query_params.get("error"))
    return templates.TemplateResponse(
        request,
        name,
        {"csrf_token": csrf, "logged_in": session is not None, **context},
        status_code=status_code,
    )


def redirect(path: str, **params: str) -> RedirectResponse:
    query = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def _set_cookie(response: Response, state: AppState, name: str, value: str, max_age: int) -> None:
    response.set_cookie(
        name,
        value,
        max_age=max_age,
        httponly=True,
        secure=state.settings.cookie_secure,
        samesite="strict",
        path="/",
    )


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, state: AppState = Depends(app_state)) -> HTMLResponse:
    cookie, login_session = state.login_sessions.issue()
    response = render(
        request, "login.html", None, login_csrf=state.login_sessions.csrf_token(login_session)
    )
    _set_cookie(response, state, LOGIN_COOKIE, cookie, 1800)
    return response


@router.post("/login")
def login(
    request: Request,
    password: str = Form(...),
    csrf_token: str = Form(""),
    state: AppState = Depends(app_state),
) -> Response:
    login_session = state.login_sessions.load(request.cookies.get(LOGIN_COOKIE))
    if login_session is None or not state.login_sessions.check_csrf(login_session, csrf_token):
        return redirect("/login", error="Your login form expired. Please try again.")
    if not verify_password(state.settings.admin_password_hash, password):
        return redirect("/login", error="Wrong password.")
    cookie, _ = state.sessions.issue()
    response = redirect("/")
    _set_cookie(response, state, SESSION_COOKIE, cookie, state.settings.session_hours * 3600)
    response.delete_cookie(LOGIN_COOKIE, path="/")
    return response


@router.post("/logout")
def logout(_session: Session = Depends(require_admin)) -> Response:
    response = redirect("/login")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/")
def home(_session: Session = Depends(require_admin)) -> Response:
    return redirect("/players")


@router.get("/players", response_class=HTMLResponse)
def players_page(
    request: Request,
    session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> HTMLResponse:
    return render(
        request,
        "players.html",
        session,
        players=store.list_players(),
        playlists=store.list_playlists(),
    )


# ------------------------------------------------------------------ media


@router.get("/media", response_class=HTMLResponse)
def media_page(
    request: Request,
    session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> HTMLResponse:
    return render(request, "media.html", session, media=store.list_media())


@router.put("/media/upload")
async def upload_media(
    request: Request,
    filename: str = Query(..., max_length=255),
    _session: Session = Depends(require_admin),
    state: AppState = Depends(app_state),
    store: Store = Depends(get_store),
) -> JSONResponse:
    """Raw-body streaming upload (sent by app.js with fetch)."""
    try:
        tmp, sha256, size = await media.receive_upload(request.stream(), state.settings)
        media_id, duplicate = await run_in_threadpool(
            media.ingest, store, state.settings, tmp, sha256, size, filename
        )
    except media.Rejected as exc:
        return JSONResponse({"detail": str(exc)}, status_code=422)
    return JSONResponse({"id": media_id, "sha256": sha256, "duplicate": duplicate})


@router.post("/media/{media_id}/delete")
def delete_media(
    media_id: int,
    _session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> Response:
    store.delete_media(media_id)
    return redirect(
        "/media",
        notice="Media removed from the library. Playlists using it keep playing it.",
    )
