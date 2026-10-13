"""Application factory. Run with a single uvicorn worker:

uvicorn coral_dashboard.main:create_app --factory --workers 1
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import api, web
from .auth import SessionSigner
from .config import Settings, load_env_file, settings_from_env
from .db import connect, migrate
from .deps import AppState, LoginRequired
from .manifest import ManifestContext
from .repo import Store

STATIC_DIR = Path(__file__).parent / "static"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; media-src 'self'; object-src 'none';"
        " frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}


def init_storage(settings: Settings, ctx: ManifestContext) -> None:
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    settings.incoming_dir.mkdir(parents=True, exist_ok=True)
    conn = connect(settings.db_path)
    try:
        migrate(conn)
        Store(conn, ctx).refresh_all_hashes()
    finally:
        conn.close()


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        load_env_file(Path(".env"))
        settings = settings_from_env()
    ctx = ManifestContext(settings.timezone, settings.default_image_duration_s)
    init_storage(settings, ctx)

    app = FastAPI(title="CORAL", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.coral = AppState(
        settings=settings,
        ctx=ctx,
        sessions=SessionSigner(settings.secret_key, settings.session_hours * 3600),
        login_sessions=SessionSigner(settings.secret_key, 1800, purpose="login"),
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, _exc: LoginRequired):
        if request.method == "GET":
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"detail": "Login required"}, status_code=401)

    app.include_router(api.router)
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
