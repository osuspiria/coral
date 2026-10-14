"""Fake mpv JSON IPC server over TCP, modelling the playlist semantics we rely on.

Modelled behaviour (to be confirmed against a real mpv, see test_mpv_real.py):
- `playlist-clear` removes every entry except the current one;
- `loadfile url append` adds to the end; `append-play` also starts playback if idle;
  `replace` drops the playlist and restarts playback (what we must never use);
- `playlist-remove N` drops entry N; removing the current entry advances;
- `playlist-next force` advances; at the end of a file, playback advances with
  `--loop-playlist=inf` and a `start-file` event carries `playlist_entry_id`.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
from dataclasses import dataclass, field
from typing import Any


class Unavailable(Exception):
    pass


@dataclass
class Entry:
    id: int
    filename: str
    options: str = ""


@dataclass
class FakeMpv:
    playlist: list[Entry] = field(default_factory=list)
    current: int | None = None  # index into playlist
    commands: list[Any] = field(default_factory=list)
    starts: list[Entry] = field(default_factory=list)  # every start-file, in order
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _writers: list[asyncio.StreamWriter] = field(default_factory=list)
    _server: asyncio.base_events.Server | None = None
    port: int = 0
    time_pos: float | None = None  # what "time-pos" answers; tests move it forward

    async def start(self, initial: list[str] | None = None) -> FakeMpv:
        for name in initial or []:
            self.playlist.append(Entry(next(self._ids), name))
        if self.playlist:
            self.current = 0
            self.starts.append(self.playlist[0])
        self._server = await asyncio.start_server(self._client, "127.0.0.1", self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    @property
    def address(self) -> str:
        return f"tcp:127.0.0.1:{self.port}"

    async def stop(self) -> None:
        """Simulate mpv dying: close every client connection."""
        for writer in self._writers:
            writer.close()
        self._writers.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    # ------------------------------------------------------------ model

    @property
    def current_entry(self) -> Entry | None:
        return None if self.current is None else self.playlist[self.current]

    def filenames(self) -> list[str]:
        return [e.filename for e in self.playlist]

    def _start(self, index: int | None) -> None:
        self.current = index
        if index is None:
            self._emit({"event": "idle"})
            return
        entry = self.playlist[index]
        self.starts.append(entry)
        self._emit({"event": "start-file", "playlist_entry_id": entry.id})

    def finish_current(self) -> None:
        """The current file reached its end; advance like --loop-playlist=inf."""
        if self.current is None:
            return
        self._emit({"event": "end-file", "reason": "eof"})
        self._start((self.current + 1) % len(self.playlist))

    def _emit(self, message: dict[str, Any]) -> None:
        data = (json.dumps(message) + "\n").encode()
        for writer in self._writers:
            writer.write(data)

    def _execute(self, command: Any) -> Any:
        if isinstance(command, dict):
            name = command["name"]
            args = command
        else:
            name, rest = command[0], command[1:]
            args = {"_": rest}
        if name == "get_property":
            return self._property(args["_"][0])
        if name == "playlist-clear":
            keep = self.current_entry
            self.playlist = [keep] if keep else []
            self.current = 0 if keep else None
            return None
        if name == "loadfile":
            url = args.get("url") or args["_"][0]
            flags = args.get("flags", "replace")
            entry = Entry(next(self._ids), url, args.get("options", ""))
            if flags == "replace":
                self.playlist = [entry]
                self._start(0)
            elif flags in {"append", "append-play"}:
                self.playlist.append(entry)
                if flags == "append-play" and self.current is None:
                    self._start(len(self.playlist) - 1)
            else:
                raise ValueError(flags)
            return None
        if name == "playlist-remove":
            target = args["_"][0]
            index = self.current if target == "current" else int(target)
            if index is None or not 0 <= index < len(self.playlist):
                raise IndexError(index)
            was_current = index == self.current
            del self.playlist[index]
            if self.current is not None and index < self.current:
                self.current -= 1
            if was_current:
                self._start(index % len(self.playlist) if self.playlist else None)
            return None
        if name == "playlist-next":
            if self.current is not None:
                self._start((self.current + 1) % len(self.playlist))
            return None
        raise KeyError(name)

    def _property(self, name: str) -> Any:
        if name == "playlist":
            return [
                {
                    "filename": e.filename,
                    "id": e.id,
                    **({"current": True, "playing": True} if i == self.current else {}),
                }
                for i, e in enumerate(self.playlist)
            ]
        if name == "time-pos":
            if self.current_entry is None or self.time_pos is None:
                raise Unavailable("property unavailable")
            return self.time_pos
        if name == "mpv-version":
            return "mpv fake"
        if name == "idle-active":
            return self.current_entry is None
        if name == "path":
            if self.current_entry is None:
                raise Unavailable("property unavailable")
            return self.current_entry.filename
        raise Unavailable("property not found")

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        with contextlib.suppress(ConnectionError):
            while line := await reader.readline():
                request = json.loads(line)
                command = request["command"]
                self.commands.append(command)
                reply: dict[str, Any] = {"request_id": request.get("request_id", 0)}
                try:
                    reply.update(error="success", data=self._execute(command))
                except Unavailable as exc:
                    reply["error"] = str(exc)
                except (KeyError, ValueError, IndexError):
                    reply["error"] = "invalid parameter"
                writer.write((json.dumps(reply) + "\n").encode())
                await writer.drain()
