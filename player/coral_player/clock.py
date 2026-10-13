"""System clock sanity (spec §4.5). The Pi has no RTC: after a boot without
network the time can be far off. With an invalid clock the player keeps the last
applied list instead of filtering by dates."""

from __future__ import annotations

from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime

from ._build import BUILD_DATE


def parse_http_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class ClockGuard:
    """Tracks the latest server time observation and decides if the clock is usable.

    Until the heartbeat exists (phase 2) the server time comes from the HTTP
    `Date` header of manifest responses.
    """

    def __init__(self, max_skew_s: float, build_date: date | None = None) -> None:
        self.max_skew_s = max_skew_s
        self.build_date = build_date or date.fromisoformat(BUILD_DATE)
        self.skew_s: float | None = None  # local - server, from the last observation

    def observe_server_time(self, server_time: datetime | None, local_now: datetime) -> None:
        if server_time is not None:
            self.skew_s = (local_now - server_time).total_seconds()

    def problem(self, now_utc: datetime) -> str | None:
        """None if the clock is usable, otherwise a short reason."""
        if now_utc.date() < self.build_date:
            return f"system time {now_utc.isoformat()} is before the build date {self.build_date}"
        if self.skew_s is not None and abs(self.skew_s) > self.max_skew_s:
            return f"system time differs from the server by {self.skew_s:+.0f} s"
        return None
