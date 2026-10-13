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
from .repo import Conflict, Store

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


# -------------------------------------------------------------- playlists

EMPTY_ASSIGNED_WARNING = "This won't clear the screens. Use Clear screen if that's what you want."


def _opt_str(value: str) -> str | None:
    value = value.strip()
    return value or None


def _opt_int(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{value!r} is not a whole number") from exc


@router.get("/playlists", response_class=HTMLResponse)
def playlists_page(
    request: Request,
    session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> HTMLResponse:
    return render(request, "playlists.html", session, playlists=store.list_playlists())


@router.post("/playlists")
def create_playlist(
    name: str = Form(""),
    _session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> Response:
    try:
        playlist_id = store.create_playlist(name)
    except ValueError as exc:
        return redirect("/playlists", error=str(exc))
    return redirect(f"/playlists/{playlist_id}", notice="Playlist created.")


@router.get("/playlists/{playlist_id}", response_class=HTMLResponse)
def playlist_page(
    playlist_id: int,
    request: Request,
    session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> HTMLResponse:
    playlist = store.get_playlist(playlist_id)
    items = store.list_items(playlist_id)
    players = store.players_using(playlist_id)
    return render(
        request,
        "playlist.html",
        session,
        playlist=playlist,
        items=items,
        players=players,
        media=store.list_media(),
        empty_warning=EMPTY_ASSIGNED_WARNING if players and not items else None,
        default_image_duration=request.app.state.coral.settings.default_image_duration_s,
        timezone=request.app.state.coral.settings.timezone,
    )


@router.post("/playlists/{playlist_id}/rename")
def rename_playlist(
    playlist_id: int,
    name: str = Form(""),
    _session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> Response:
    try:
        store.rename_playlist(playlist_id, name)
    except ValueError as exc:
        return redirect(f"/playlists/{playlist_id}", error=str(exc))
    return redirect(f"/playlists/{playlist_id}", notice="Playlist renamed.")


@router.post("/playlists/{playlist_id}/delete")
def delete_playlist(
    playlist_id: int,
    _session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> Response:
    try:
        store.delete_playlist(playlist_id)
    except Conflict as exc:
        return redirect(f"/playlists/{playlist_id}", error=str(exc))
    return redirect("/playlists", notice="Playlist deleted.")


@router.post("/playlists/{playlist_id}/items")
def add_item(
    playlist_id: int,
    media_id: int = Form(...),
    start_date: str = Form(""),
    end_date: str = Form(""),
    image_duration_s: str = Form(""),
    _session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> Response:
    try:
        store.add_item(
            playlist_id,
            media_id,
            _opt_str(start_date),
            _opt_str(end_date),
            _opt_int(image_duration_s),
        )
    except (ValueError, Conflict) as exc:
        return redirect(f"/playlists/{playlist_id}", error=str(exc))
    return redirect(f"/playlists/{playlist_id}", notice="Item added.")


@router.post("/items/{item_id}/update")
def update_item(
    item_id: int,
    start_date: str = Form(""),
    end_date: str = Form(""),
    image_duration_s: str = Form(""),
    _session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> Response:
    playlist_id = store.get_item(item_id)["playlist_id"]
    try:
        store.update_item(
            item_id, _opt_str(start_date), _opt_str(end_date), _opt_int(image_duration_s)
        )
    except ValueError as exc:
        return redirect(f"/playlists/{playlist_id}", error=str(exc))
    return redirect(f"/playlists/{playlist_id}", notice="Item updated.")


@router.post("/items/{item_id}/move")
def move_item(
    item_id: int,
    position: str = Form(""),
    _session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> Response:
    playlist_id = store.get_item(item_id)["playlist_id"]
    try:
        new_position = _opt_int(position)
        if new_position is None:
            raise ValueError("Enter a position")
        store.move_item(item_id, new_position)
    except ValueError as exc:
        return redirect(f"/playlists/{playlist_id}", error=str(exc))
    return redirect(f"/playlists/{playlist_id}")


@router.post("/items/{item_id}/remove")
def remove_item(
    item_id: int,
    _session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> Response:
    playlist_id = store.get_item(item_id)["playlist_id"]
    store.remove_item(item_id)
    if not store.list_items(playlist_id) and store.players_using(playlist_id):
        return redirect(f"/playlists/{playlist_id}", error=EMPTY_ASSIGNED_WARNING)
    return redirect(f"/playlists/{playlist_id}", notice="Item removed.")


# ---------------------------------------------------------------- players


@router.post("/players/{player_id}/assign")
def assign_playlist(
    player_id: int,
    playlist_id: str = Form(""),
    _session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> Response:
    try:
        target = _opt_int(playlist_id)
        if target is None:
            raise ValueError("Choose a playlist")
        store.assign_playlist(player_id, target)
    except ValueError as exc:
        return redirect("/players", error=str(exc))
    notice = "Playlist assigned."
    if not store.list_items(target):
        notice += " " + EMPTY_ASSIGNED_WARNING
    return redirect("/players", notice=notice)


@router.post("/players/{player_id}/unassign")
def unassign_playlist(
    player_id: int,
    clear: str = Form(...),
    _session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> Response:
    """Unassigning asks "Clear the screen too?": clear=yes is an explicit clear,
    clear=no keeps the player's last list."""
    if clear not in {"yes", "no"}:
        return redirect("/players", error="Choose whether to clear the screen.")
    store.unassign_playlist(player_id, clear=clear == "yes")
    if clear == "yes":
        return redirect("/players", notice="Playlist unassigned and screen cleared.")
    return redirect("/players", notice="Playlist unassigned. The player keeps its last list.")


@router.post("/players/{player_id}/clear")
def clear_screen(
    player_id: int,
    _session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> Response:
    store.set_cleared(player_id, True)
    return redirect("/players", notice="Screen cleared. The player will show the fallback image.")


@router.post("/players/{player_id}/resume")
def resume_screen(
    player_id: int,
    _session: Session = Depends(require_admin),
    store: Store = Depends(get_store),
) -> Response:
    store.set_cleared(player_id, False)
    return redirect("/players", notice="Screen resumed.")
