"""Date filtering of playlist items (spec §4.5): inclusive, in the manifest timezone."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from .manifest import Item
from .state import PlayEntry


def is_active(item: Item, today: date) -> bool:
    if item.start_date is not None and today < item.start_date:
        return False
    return not (item.end_date is not None and today > item.end_date)


def active_entries(items: Iterable[Item], today: date) -> list[PlayEntry]:
    """Items valid today, in playlist order, as mpv play entries."""
    return [
        PlayEntry(i.sha256, i.ext, i.kind, i.duration if i.kind == "image" else None)
        for i in items
        if is_active(i, today)
    ]


def local_today(now_utc: datetime, tz: ZoneInfo) -> date:
    return now_utc.astimezone(tz).date()


def seconds_until_next_evaluation(now_utc: datetime, tz: ZoneInfo, interval_s: float) -> float:
    """Re-evaluate every `interval_s`, and also right after the next local midnight."""
    local = now_utc.astimezone(tz)
    next_midnight = datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=tz)
    until_midnight = (next_midnight - local).total_seconds() + 1
    return max(1.0, min(interval_s, until_midnight))
