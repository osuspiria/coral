"""When to sync. Since phase 2 the heartbeat drives it (spec 5.1, 4.4).

The agent only depends on the `ChangeSource` protocol:

- `next_trigger()` waits until a sync should run and says whether it must be a
  full check (ignore the ETag and re-check every file);
- `sync_finished(ok)` feeds the result back, so failures back off;
- `wake()` asks for an immediate sync (used by `sync-now`).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SyncTrigger:
    full: bool
    reason: str


class ChangeSource(Protocol):
    async def next_trigger(self) -> SyncTrigger: ...

    def sync_finished(self, ok: bool) -> None: ...

    def wake(self, full: bool = False) -> None: ...

    def notify_hash(self, desired: str | None, seen: str | None) -> None: ...


class Backoff:
    """Exponential backoff with jitter: a random delay in [d/2, d], d = min * 2^n."""

    def __init__(self, min_s: float, max_s: float, rng: random.Random | None = None) -> None:
        self.min_s, self.max_s = min_s, max_s
        self.failures = 0
        self._rng = rng or random.Random()  # noqa: S311 (jitter, not security)

    def reset(self) -> None:
        self.failures = 0

    def fail(self) -> None:
        self.failures += 1

    def delay(self) -> float:
        ceiling = min(self.max_s, self.min_s * 2 ** max(self.failures - 1, 0))
        return self._rng.uniform(ceiling / 2, ceiling)


class HeartbeatChangeSource:
    """Syncs when the heartbeat reports a playlist hash the player has not handled
    yet (`notify_hash`), on `sync-now` (`wake(full=True)`), at startup, and every
    `full_interval_s` as a full check. Failures are retried with backoff, without
    waiting for another heartbeat.

    The heartbeat loop runs separately from syncing, so the player keeps reporting
    while a long download is in progress.
    """

    def __init__(
        self,
        full_interval_s: float,
        backoff: Backoff,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.full_interval_s = full_interval_s
        self.backoff = backoff
        self._monotonic = monotonic
        self._wake = asyncio.Event()
        self._force_full = True  # the first sync after start is always a full check
        self._first = True
        self._last_full: float | None = None
        self._pending_full = False
        self._requested_hash: str | None = None

    def wake(self, full: bool = False) -> None:
        self._force_full = self._force_full or full
        self._wake.set()

    def notify_hash(self, desired: str | None, seen: str | None) -> None:
        """Called on every heartbeat. Wakes the sync once per new desired hash; a
        failed sync is retried by the backoff, not by every following heartbeat."""
        if desired is None or desired == seen or desired == self._requested_hash:
            return
        self._requested_hash = desired
        self.wake()

    def next_delay(self) -> float:
        if self.backoff.failures:
            return self.backoff.delay()
        if self._last_full is None:
            return self.full_interval_s
        elapsed = self._monotonic() - self._last_full
        return max(0.0, self.full_interval_s - elapsed)

    async def next_trigger(self) -> SyncTrigger:
        if self._first:
            self._first = False
            reason = "startup"
        else:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.next_delay())
                reason = "heartbeat"
            except TimeoutError:
                reason = "backoff retry" if self.backoff.failures else "periodic full check"
        self._wake.clear()
        due = self._last_full is None or (
            self._monotonic() - self._last_full >= self.full_interval_s
        )
        full = self._force_full or due
        self._force_full = False
        self._pending_full = full
        return SyncTrigger(full=full, reason=reason)

    def sync_finished(self, ok: bool) -> None:
        if ok:
            self.backoff.reset()
            if self._pending_full:
                self._last_full = self._monotonic()
        else:
            self.backoff.fail()
            if self._pending_full:
                self._force_full = True  # retry the full check, not just a fetch
