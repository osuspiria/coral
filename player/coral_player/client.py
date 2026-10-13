"""HTTPS client for the dashboard player API (stdlib only).

Short timeouts; every failure is raised as a typed exception so the caller can
back off and report it. Nothing here touches what is playing.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__
from .clock import parse_http_date

log = logging.getLogger(__name__)

MAX_MANIFEST_BYTES = 5 * 1024 * 1024
CHUNK = 256 * 1024


class ServerError(Exception):
    """The request did not produce a usable answer."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class Unauthorized(ServerError):
    """401: the token is invalid or revoked. Keep playing the cache."""


@dataclass(frozen=True)
class ManifestResponse:
    changed: bool  # False on 304
    raw: Any | None
    etag: str | None
    server_time: datetime | None


class ServerClient:
    def __init__(self, base_url: str, token: str, timeout_s: float) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout_s = timeout_s
        # No automatic redirects to other hosts: the player only talks to its server.
        self._opener = urllib.request.build_opener(_NoRedirect)

    def _request(self, path: str, headers: dict[str, str] | None = None) -> urllib.request.Request:
        if not path.startswith("/api/v1/"):
            raise ServerError(f"refusing unexpected path {path!r}")
        return urllib.request.Request(  # noqa: S310 (scheme validated in config)
            self.base_url + path,
            headers={
                "Authorization": f"Bearer {self._token}",
                "User-Agent": f"coral-player/{__version__}",
                **(headers or {}),
            },
        )

    def _open(self, request: urllib.request.Request):
        try:
            return self._opener.open(request, timeout=self.timeout_s)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return exc
            exc.close()
            if exc.code == 401:
                raise Unauthorized("token rejected by the server (401)", 401) from None
            raise ServerError(f"HTTP {exc.code} for {request.full_url}", exc.code) from None
        except (urllib.error.URLError, OSError) as exc:
            raise ServerError(f"network error: {exc}") from exc

    def fetch_manifest(self, if_none_match: str | None) -> ManifestResponse:
        headers = {"Accept": "application/json"}
        if if_none_match:
            headers["If-None-Match"] = f'"{if_none_match}"'
        response = self._open(self._request("/api/v1/manifest", headers))
        with response:
            server_time = parse_http_date(response.headers.get("Date"))
            etag = (response.headers.get("ETag") or "").strip().removeprefix("W/").strip('"')
            if response.status == 304:
                return ManifestResponse(False, None, etag or if_none_match, server_time)
            body = response.read(MAX_MANIFEST_BYTES + 1)
        if len(body) > MAX_MANIFEST_BYTES:
            raise ServerError("manifest too large")
        try:
            raw = json.loads(body)
        except ValueError as exc:
            raise ServerError("manifest is not valid JSON") from exc
        return ManifestResponse(True, raw, etag or None, server_time)

    def download(self, url_path: str, part: Path, expected_size: int) -> None:
        """Download into `part`, resuming with Range from its current size."""
        have = part.stat().st_size if part.exists() else 0
        if have > expected_size:
            part.unlink()
            have = 0
        if have == expected_size:
            return
        headers = {"Range": f"bytes={have}-"} if have else {}
        response = self._open(self._request(url_path, headers))
        with response:
            if response.status == 206:
                content_range = response.headers.get("Content-Range", "")
                if not content_range.startswith(f"bytes {have}-"):
                    raise ServerError(f"unexpected Content-Range {content_range!r}")
                mode = "ab"
            elif response.status == 200:
                mode, have = "wb", 0
            else:
                raise ServerError(f"unexpected HTTP {response.status} for media")
            written = have
            # Bytes received before a failure stay in `part` so the next try resumes.
            with part.open(mode) as fh:
                try:
                    while chunk := response.read(CHUNK):
                        written += len(chunk)
                        if written > expected_size:
                            raise ServerError("server sent more bytes than the manifest size")
                        fh.write(chunk)
                except (http.client.HTTPException, OSError) as exc:
                    raise ServerError(f"download interrupted after {written} bytes: {exc}") from exc
                finally:
                    fh.flush()
                    os.fsync(fh.fileno())
            if written < expected_size:
                raise ServerError(f"download interrupted after {written} of {expected_size} bytes")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        return None


def utc_now() -> datetime:
    return datetime.now(UTC)
