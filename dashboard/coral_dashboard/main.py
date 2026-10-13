"""Application factory. Run with a single uvicorn worker:

uvicorn coral_dashboard.main:create_app --factory --workers 1
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import api, media, web
from .auth import SessionSigner
from .config import Settings, load_env_file, settings_from_env
from .db import connect, migrate
from .deps import AppState, LoginRequired
from .manifest import ManifestContext
from .repo import NotFound, Store

log = logging.getLogger(__name__)

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


def _collect_garbage(settings: Settings, ctx: ManifestContext) -> None:
    conn = connect(settings.db_path)
    try:
        media.collect_garbage(Store(conn, ctx), settings)
    finally:
        conn.close()


async def _gc_loop(settings: Settings, ctx: ManifestContext) -> None:
    """Daily media garbage collection. Safe because there is only one worker."""
    while True:
        try:
            await asyncio.to_thread(_collect_garbage, settings, ctx)
        except Exception:
            log.exception("Media garbage collection failed")
        await asyncio.sleep(24 * 3600)


def create_app(settings: Settings | None = None) -> FastAPI:
    if settings is None:
        load_env_file(Path(".env"))
        settings = settings_from_env()
    ctx = ManifestContext(settings.timezone, settings.default_image_duration_s)
    init_storage(settings, ctx)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(_gc_loop(settings, ctx))
        yield
        task.cancel()

    app = FastAPI(title="CORAL", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
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

    @app.exception_handler(NotFound)
    async def not_found(_request: Request, exc: NotFound):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    app.include_router(api.router)
    app.include_router(web.router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
