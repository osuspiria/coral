"""Player API (Bearer token): manifest and media download (spec §5.3)."""

from __future__ import annotations

import re
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .deps import AppState, app_state, get_store, require_player
from .repo import Store

router = APIRouter(prefix="/api/v1")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

MEDIA_TYPES = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
}


def _etag_matches(header: str | None, etag: str) -> bool:
    if not header:
        return False
    candidates = [c.strip().removeprefix("W/").strip('"') for c in header.split(",")]
    return etag in candidates or "*" in candidates


@router.get("/manifest")
def get_manifest(
    request: Request,
    player: sqlite3.Row = Depends(require_player),
    store: Store = Depends(get_store),
) -> Response:
    """Always 200 (or 304) for a valid token. `assigned` and `cleared` tell the
    player what an empty item list means; see spec §4.4."""
    etag = store.player_etag(player)
    headers = {"Cache-Control": "no-cache"}
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={**headers, "ETag": f'"{etag}"'})
    manifest = store.player_manifest(player)
    headers["ETag"] = f'"{manifest["playlist_hash"]}"'
    return JSONResponse(manifest, headers=headers)


@router.get("/media/{sha256}")
def get_media(
    sha256: str,
    player: sqlite3.Row = Depends(require_player),
    store: Store = Depends(get_store),
    state: AppState = Depends(app_state),
) -> Response:
    """Serve a media file to a player whose playlist contains it. Supports Range."""
    if not _SHA256.match(sha256) or not store.player_may_download(player, sha256):
        raise HTTPException(404, "Not found")
    media = store.get_media_by_sha(sha256)
    path = state.settings.media_dir / media["stored_name"] if media else None
    if path is None or not path.is_file():
        raise HTTPException(404, "Not found")
    ext = media["stored_name"].rsplit(".", 1)[-1]
    return FileResponse(
        path,
        media_type=MEDIA_TYPES.get(ext, "application/octet-stream"),
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


# ---------------------------------------------------------------- heartbeat

MAX_HEARTBEAT_BYTES = 16 * 1024


class _Lenient(BaseModel):
    # Newer agents may send more fields; only the known ones are stored.
    model_config = ConfigDict(extra="ignore")


class MpvReport(_Lenient):
    state: Literal["playing", "idle", "stuck", "disconnected"]
    current_file: str | None = Field(None, max_length=200)
    progressing: bool = False
    restarts: int = Field(0, ge=0)


class SyncReport(_Lenient):
    last_ok: str | None = Field(None, max_length=64)
    last_error: str | None = Field(None, max_length=500)


class UpdateReport(_Lenient):
    state: str = Field("idle", max_length=32)
    last_error: str | None = Field(None, max_length=500)


class SystemReport(_Lenient):
    disk_free_mb: int | None = Field(None, ge=0)
    temp_c: float | None = None
    uptime_s: int | None = Field(None, ge=0)


class HeartbeatIn(_Lenient):
    agent_version: str = Field(max_length=32)
    player_time: str = Field(max_length=64)
    playlist_hash_applied: str | None = Field(None, max_length=64)
    playlist_hash_seen: str | None = Field(None, max_length=64)
    cleared: bool = False
    clock_drift: bool = False
    mpv: MpvReport
    sync: SyncReport
    update: UpdateReport = UpdateReport()
    system: SystemReport = SystemReport()
    acked_commands: list[int] = Field(default_factory=list, max_length=100)


def _limit_body(request: Request) -> None:
    length = request.headers.get("content-length")
    if length is None or not length.isdigit() or int(length) > MAX_HEARTBEAT_BYTES:
        raise HTTPException(413, "Heartbeat too large or without Content-Length")


@router.post("/heartbeat", dependencies=[Depends(_limit_body)])
def heartbeat(
    body: HeartbeatIn,
    player: sqlite3.Row = Depends(require_player),
    store: Store = Depends(get_store),
    state: AppState = Depends(app_state),
) -> JSONResponse:
    """Spec 5.1: records the player's state, confirms acked commands and returns
    the desired playlist hash and the open commands."""
    now = state.clock()
    store.record_heartbeat(player["id"], body.model_dump(), now)
    if body.acked_commands:
        store.ack_commands(player["id"], body.acked_commands, now)
    commands = store.deliver_commands(player["id"], now)
    return JSONResponse(
        {
            "server_time": now.isoformat(),
            "playlist_hash": store.player_etag(player),
            "desired_version": None,  # phase 3
            "commands": [{"id": c["id"], "name": c["name"]} for c in commands],
            "heartbeat_interval_s": state.settings.heartbeat_interval_s,
        },
        headers={"Cache-Control": "no-store"},
    )
