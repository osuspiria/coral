"""mpv health (spec 4.6): is the screen really changing?

Progress means `time-pos` advancing or a new file starting (for images mpv keeps
`time-pos` at 0, so only the next file counts). Without progress for
longer than the stall limit, mpv is considered stuck and restarted. The limit is
at least 2 minutes and grows with the duration of the image on screen, so a
long-running image is never mistaken for a stall.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

MIN_STALL_S = 120.0
STALL_MARGIN_S = 30.0
IDLE_CONFIRMATIONS = 2  # consecutive idle checks before acting
MAX_IDLE_RELOADS = 3  # reloads of an idle mpv before restarting it


@dataclass
class MpvReport:
    state: str  # playing | idle | stuck | disconnected
    current_file: str | None
    progressing: bool
    restarts: int

    def to_json(self) -> dict:
        return {
            "state": self.state,
            "current_file": self.current_file,
            "progressing": self.progressing,
            "restarts": self.restarts,
        }


class MpvHealth:
    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self.restarts = 0
        self.stuck = False
        self.connected = False
        self.idle = False
        self.current_file: str | None = None
        self._last_pos: float | None = None
        self._last_progress = monotonic()
        self.idle_checks = 0  # consecutive checks that found mpv idle
        self.idle_reloads = 0  # reloads since mpv was last seen not idle

    def _progress(self) -> None:
        self._last_progress = self._monotonic()
        self.stuck = False

    def on_connect(self) -> None:
        self.connected = True
        self._last_pos = None
        self._progress()

    def on_disconnect(self) -> None:
        self.connected = False

    def on_start_file(self) -> None:
        self._last_pos = None
        self._progress()

    def observe(self, time_pos: float | None, current_file: str | None, idle: bool) -> None:
        self.idle = idle
        if idle:
            self.idle_checks += 1
        else:
            self.idle_checks = 0
            self.idle_reloads = 0
        if current_file != self.current_file:
            self.current_file = current_file
            self._progress()
        if time_pos is not None and time_pos != self._last_pos:
            self._last_pos = time_pos
            self._progress()

    def seconds_without_progress(self) -> float:
        return self._monotonic() - self._last_progress

    def is_stalled(self, image_duration_s: float | None) -> bool:
        """True when mpv should be restarted. Idle mpv (nothing loaded) is not a stall
        of playback; the agent reloads the list instead."""
        if not self.connected or self.idle:
            return False
        limit = max(MIN_STALL_S, (image_duration_s or 0) + STALL_MARGIN_S)
        return self.seconds_without_progress() > limit

    def idle_action(self) -> str | None:
        """What to do about an idle mpv: None (not idle or not confirmed yet),
        "reload" the play list, or "restart" mpv after reloads did not help."""
        if not self.idle or self.idle_checks < IDLE_CONFIRMATIONS:
            return None
        if self.idle_reloads >= MAX_IDLE_RELOADS:
            return "restart"
        return "reload"

    def mark_reloaded(self) -> None:
        self.idle_reloads += 1
        self.idle_checks = 0

    def mark_restarted(self) -> None:
        self.idle_checks = 0
        self.idle_reloads = 0
        self.restarts += 1
        self.stuck = True
        self._last_progress = self._monotonic()  # give the new mpv time to come up

    def report(self, image_duration_s: float | None = None) -> MpvReport:
        """`progressing` allows for images, whose time-pos stays at 0 (checked with
        a real mpv): an image counts as progressing until its duration has passed."""
        if not self.connected:
            state = "disconnected"
        elif self.stuck:
            state = "stuck"
        elif self.idle:
            state = "idle"
        else:
            state = "playing"
        window = max(30.0, (image_duration_s or 0) + 5)
        progressing = self.connected and not self.idle and self.seconds_without_progress() < window
        return MpvReport(state, self.current_file, progressing, self.restarts)
