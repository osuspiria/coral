"""Local player state persisted in state.json (spec §4.2).

Nothing critical lives only in memory: what is on screen and why survives a
power cut and is re-applied on boot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_json, read_json


@dataclass(frozen=True)
class PlayEntry:
    """One entry sent to mpv. An empty play list means the fallback image."""

    sha256: str
    ext: str
    kind: str
    duration: float | None

    @property
    def filename(self) -> str:
        return f"{self.sha256}.{self.ext}"


@dataclass
class State:
    # The list last sent to mpv. Kept when the clock is invalid (spec §4.5).
    playing: list[PlayEntry] = field(default_factory=list)
    # Hash of the last manifest the player acted on (applied, cleared or kept).
    seen_hash: str | None = None
    # Hash of the last promoted manifest (also in manifest.json).
    applied_hash: str | None = None
    cleared: bool = False
    last_sync_ok: str | None = None
    last_sync_error: str | None = None
    last_heartbeat_ok: str | None = None
    last_heartbeat_error: str | None = None
    clock_drift: bool = False
    # Remote commands: ids already executed (never run twice) and acks still to send.
    executed_command_ids: list[int] = field(default_factory=list)
    pending_acks: list[int] = field(default_factory=list)
    # sha256-named files no longer referenced, with the time they became unused.
    orphaned_since: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["playing"] = [asdict(p) for p in self.playing]
        return data

    @classmethod
    def from_json(cls, data: Any) -> State:
        if not isinstance(data, dict):
            return cls()
        state = cls()
        try:
            state.playing = [PlayEntry(**p) for p in data.get("playing", [])]
        except TypeError:
            state.playing = []
        for name in (
            "seen_hash",
            "applied_hash",
            "last_sync_ok",
            "last_sync_error",
            "last_heartbeat_ok",
            "last_heartbeat_error",
        ):
            value = data.get(name)
            setattr(state, name, value if isinstance(value, str) else None)
        state.cleared = bool(data.get("cleared", False))
        state.clock_drift = bool(data.get("clock_drift", False))
        for name in ("executed_command_ids", "pending_acks"):
            values = data.get(name, [])
            if isinstance(values, list):
                ints = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
                setattr(state, name, ints)
        orphaned = data.get("orphaned_since", {})
        if isinstance(orphaned, dict):
            state.orphaned_since = {str(k): str(v) for k, v in orphaned.items()}
        return state


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> State:
        return State.from_json(read_json(self.path))

    def save(self, state: State) -> None:
        atomic_write_json(self.path, state.to_json())
