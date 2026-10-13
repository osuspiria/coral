"""Agent side of the update protocol (spec 6.2, 6.5).

The agent has no privileges: it only asks for a version (request.json) and, once
the new release has started healthy, confirms it (commit.json). The root
updater does the rest and publishes its state in status.json, which the agent
reports in the heartbeat.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from .fsutil import atomic_write_json, read_json

log = logging.getLogger(__name__)

VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
UPDATER_STATE = Path("/var/lib/signage-updater")


class UpdateClient:
    def __init__(
        self, data_dir: Path, running_version: str, updater_state: Path = UPDATER_STATE
    ) -> None:
        self.dir = data_dir / "update"
        self.running = running_version
        self.updater_state = updater_state
        self.committed = False

    def updater_status(self) -> dict[str, Any]:
        status = read_json(self.updater_state / "status.json")
        return status if isinstance(status, dict) else {}

    def report(self) -> dict[str, Any]:
        """The heartbeat's `update` object."""
        status = self.updater_status()
        state = status.get("state") or "idle"
        if state == "committed" and status.get("version") == self.running:
            state = "idle"  # nothing in progress
        return {
            "state": str(state)[:32],
            "version": status.get("version"),
            "last_error": status.get("last_error"),
        }

    def want(self, desired: str | None) -> None:
        """Ask the updater for `desired` (or withdraw the request)."""
        path = self.dir / "request.json"
        if desired is None or desired == self.running or not VERSION_RE.fullmatch(desired):
            path.unlink(missing_ok=True)
            return
        status = self.updater_status()
        if status.get("version") == desired and status.get("state") in {"rejected", "rolled_back"}:
            return  # already failed here; the server stops offering it after the report
        current = read_json(path)
        if isinstance(current, dict) and current.get("version") == desired:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, {"version": desired})
        log.warning("Requested software update to %s (running %s)", desired, self.running)

    def maybe_commit(self, heartbeat_accepted: bool, playing: bool) -> bool:
        """Confirm the running release once the server accepted a heartbeat from it
        and mpv is playing (spec 6.2.5). Returns True when a commit was written."""
        if self.committed or not (heartbeat_accepted and playing):
            return False
        status = self.updater_status()
        if status.get("state") != "pending_commit" or status.get("version") != self.running:
            return False
        nonce = status.get("nonce")
        if not isinstance(nonce, str):
            return False
        self.dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.dir / "commit.json", {"version": self.running, "nonce": nonce})
        self.committed = True
        log.warning("Confirmed a healthy start of %s", self.running)
        return True
