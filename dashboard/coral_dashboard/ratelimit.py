"""Login throttling per client IP (spec 7): rate limit with a progressive delay.

In memory, which is enough with a single worker. After FREE_FAILURES failed
logins from an IP, further attempts are refused until a delay has passed; the
delay doubles with every failure up to MAX_DELAY_S. After LOCK_AFTER failures
the IP is locked out for LOCK_S. A successful login clears the record. Attempts
are refused rather than delayed server-side, so nothing blocks a worker.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

FREE_FAILURES = 4
MAX_DELAY_S = 60.0
LOCK_AFTER = 20
LOCK_S = 900.0


@dataclass
class _Record:
    failures: int
    last_failure: float


class LoginThrottle:
    def __init__(self, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._records: dict[str, _Record] = {}

    def _required_wait(self, record: _Record) -> float:
        if record.failures >= LOCK_AFTER:
            return LOCK_S
        if record.failures <= FREE_FAILURES:
            return 0.0
        return min(MAX_DELAY_S, 2.0 ** (record.failures - FREE_FAILURES - 1))

    def wait_seconds(self, ip: str) -> float:
        """Seconds before this IP may try again (0 = allowed now)."""
        record = self._records.get(ip)
        if record is None:
            return 0.0
        elapsed = self._monotonic() - record.last_failure
        if elapsed >= LOCK_S:
            del self._records[ip]  # old record: forget it
            return 0.0
        return max(0.0, self._required_wait(record) - elapsed)

    def failure(self, ip: str) -> int:
        record = self._records.setdefault(ip, _Record(0, 0.0))
        record.failures += 1
        record.last_failure = self._monotonic()
        if len(self._records) > 10000:  # bound memory under a distributed attack
            oldest = min(self._records, key=lambda k: self._records[k].last_failure)
            del self._records[oldest]
        return record.failures

    def success(self, ip: str) -> None:
        self._records.pop(ip, None)
