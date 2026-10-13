"""A small fake dashboard for player tests: manifest with ETag/304 and media with Range."""

from __future__ import annotations

import json
import threading
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class FakeServer:
    def __init__(self, token: str = "tok") -> None:
        self.token = token
        self.manifest: dict[str, Any] | None = None
        self.manifest_status = 200  # set to 404/500 to simulate failures
        self.files: dict[str, bytes] = {}
        self.corrupt: set[str] = set()  # serve wrong bytes for these sha256
        self.cut_after: dict[str, int] = {}  # close the connection after N bytes (once)
        self.server_time = None  # datetime for the Date header, None = real time
        self.requests: list[tuple[str, dict[str, str]]] = []
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def __enter__(self) -> FakeServer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()

    def paths(self) -> list[str]:
        return [p for p, _ in self.requests]

    def _handler(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def date_time_string(self, timestamp=None):
                if fake.server_time is not None:
                    return format_datetime(fake.server_time, usegmt=True)
                return super().date_time_string(timestamp)

            def _send(self, status, body=b"", headers=None):
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                fake.requests.append((self.path, dict(self.headers)))
                if self.headers.get("Authorization") != f"Bearer {fake.token}":
                    return self._send(401)
                if self.path == "/api/v1/manifest":
                    return self._manifest()
                if self.path.startswith("/api/v1/media/"):
                    return self._media(self.path.rsplit("/", 1)[1])
                return self._send(404)

            def _manifest(self):
                if fake.manifest_status != 200 or fake.manifest is None:
                    return self._send(fake.manifest_status if fake.manifest else 404)
                etag = fake.manifest.get("playlist_hash", "")
                if self.headers.get("If-None-Match") == f'"{etag}"':
                    return self._send(304, headers={"ETag": f'"{etag}"'})
                body = json.dumps(fake.manifest).encode()
                return self._send(
                    200, body, {"ETag": f'"{etag}"', "Content-Type": "application/json"}
                )

            def _media(self, sha):
                data = fake.files.get(sha)
                if data is None:
                    return self._send(404)
                if sha in fake.corrupt:
                    data = bytes([data[0] ^ 0xFF]) + data[1:]
                start = 0
                rng = self.headers.get("Range")
                status, headers = 200, {}
                if rng and rng.startswith("bytes="):
                    start = int(rng[6:].split("-")[0])
                    if start >= len(data):
                        return self._send(416)
                    status = 206
                    headers["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
                body = data[start:]
                cut = fake.cut_after.pop(sha, None)
                if cut is not None:
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body[:cut])
                    self.wfile.flush()
                    self.close_connection = True
                    return None
                return self._send(status, body, headers)

        return Handler
