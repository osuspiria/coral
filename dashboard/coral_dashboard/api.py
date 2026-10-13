"""Player API (Bearer token): manifest and media download (spec §5.3)."""

from __future__ import annotations

import re
import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

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
