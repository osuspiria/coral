"""Asynchronous client for mpv's JSON IPC (--input-ipc-server).

Addresses: "unix:/run/signage/mpv.sock" in production, "tcp:127.0.0.1:PORT" for the
fake mpv used in tests (asyncio has no AF_UNIX on Windows).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

EventHandler = Callable[[dict[str, Any]], None]


class MpvError(Exception):
    pass


class MpvIpc:
    def __init__(self, address: str, on_event: EventHandler | None = None) -> None:
        self.address = address
        self.on_event = on_event
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._ids = itertools.count(1)
        self._read_task: asyncio.Task[None] | None = None
        self.closed = asyncio.Event()
        self.closed.set()

    @property
    def connected(self) -> bool:
        return not self.closed.is_set()

    async def connect(self, timeout_s: float = 5) -> None:
        kind, _, target = self.address.partition(":")
        if kind == "unix":
            opener = asyncio.open_unix_connection(target, limit=2**22)
        elif kind == "tcp":
            host, _, port = target.rpartition(":")
            opener = asyncio.open_connection(host, int(port), limit=2**22)
        else:
            raise ValueError(f"unsupported mpv IPC address {self.address!r}")
        self._reader, self._writer = await asyncio.wait_for(opener, timeout_s)
        self.closed.clear()
        self._read_task = asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while line := await self._reader.readline():
                try:
                    message = json.loads(line)
                except ValueError:
                    log.warning("Ignoring invalid line from mpv: %r", line[:200])
                    continue
                if "event" in message:
                    if self.on_event is not None:
                        try:
                            self.on_event(message)
                        except Exception:
                            log.exception("mpv event handler failed")
                    continue
                future = self._pending.pop(message.get("request_id"), None)
                if future is not None and not future.done():
                    future.set_result(message)
        except (ConnectionError, asyncio.IncompleteReadError, OSError) as exc:
            log.warning("mpv IPC connection lost: %s", exc)
        finally:
            self._fail_pending(MpvError("mpv IPC disconnected"))
            self.closed.set()

    def _fail_pending(self, exc: Exception) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def command(self, *args: Any, timeout_s: float = 5) -> Any:
        """Positional command, e.g. command("playlist-remove", 0)."""
        return await self._send(list(args), timeout_s)

    async def command_named(self, name: str, timeout_s: float = 5, **kwargs: Any) -> Any:
        """Named-argument command. Robust to positional changes between mpv versions
        (loadfile gained an `index` argument in 0.38)."""
        return await self._send({"name": name, **kwargs}, timeout_s)

    async def get_property(self, name: str) -> Any:
        return await self.command("get_property", name)

    async def _send(self, command: Any, timeout_s: float) -> Any:
        if not self.connected or self._writer is None:
            raise MpvError("mpv IPC not connected")
        request_id = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        payload = json.dumps({"command": command, "request_id": request_id}) + "\n"
        try:
            self._writer.write(payload.encode())
            await self._writer.drain()
            reply = await asyncio.wait_for(future, timeout_s)
        except TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise MpvError(f"mpv did not answer {command!r} within {timeout_s} s") from exc
        except (ConnectionError, OSError) as exc:
            self._pending.pop(request_id, None)
            raise MpvError(f"mpv IPC write failed: {exc}") from exc
        if reply.get("error") != "success":
            raise MpvError(f"mpv command {command!r} failed: {reply.get('error')}")
        return reply.get("data")

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        if self._read_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
        self.closed.set()
