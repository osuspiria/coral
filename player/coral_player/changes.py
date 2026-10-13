"""When to sync. Phase 1 polls the manifest; phase 2 swaps in the heartbeat.

The agent only depends on the `ChangeSource` protocol:

- `next_trigger()` waits until a sync should run and says whether it must be a
  full check (ignore the ETag and re-check every file);
- `sync_finished(ok)` feeds the result back, so failures back off;
- `wake()` asks for an immediate sync (used by `sync-now` in phase 2).

`PollingChangeSource` asks for a sync on every poll; the conditional GET with
`If-None-Match: <playlist_hash>` makes an unchanged manifest cost a 304. The
phase 2 `HeartbeatChangeSource` will instead trigger only when the heartbeat's
`playlist_hash` differs from the applied one.
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


class PollingChangeSource:
    def __init__(
        self,
        interval_s: float,
        full_interval_s: float,
        backoff: Backoff,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        self.interval_s = interval_s
        self.full_interval_s = full_interval_s
        self.backoff = backoff
        self._monotonic = monotonic
        self._rng = rng or random.Random()  # noqa: S311 (jitter, not security)
        self._wake = asyncio.Event()
        self._force_full = True  # the first sync after start is always a full check
        self._first = True
        self._last_full: float | None = None
        self._pending_full = False

    def wake(self, full: bool = False) -> None:
        self._force_full = self._force_full or full
        self._wake.set()

    def next_delay(self) -> float:
        if self.backoff.failures:
            return self.backoff.delay()
        return self.interval_s * self._rng.uniform(0.9, 1.1)

    async def next_trigger(self) -> SyncTrigger:
        if self._first:
            self._first = False
            reason = "startup"
        else:
            delay = self.next_delay()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
                reason = "wake"
            except TimeoutError:
                reason = "backoff retry" if self.backoff.failures else "poll"
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
                self._force_full = True  # retry the full check, not just a poll
