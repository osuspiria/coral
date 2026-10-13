from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from coral_player.clock import ClockGuard, parse_http_date
from coral_player.manifest import parse_manifest
from coral_player.schedule import active_entries, local_today, seconds_until_next_evaluation
from player_helpers import blob, item_for, make_manifest

LISBON = ZoneInfo("Europe/Lisbon")


def manifest():
    return parse_manifest(
        make_manifest(
            [
                item_for(blob(1), 1),
                item_for(blob(2), 2, end="2026-10-13"),
                item_for(blob(3), 3, start="2026-10-14"),
                item_for(
                    blob(4),
                    4,
                    kind="video",
                    ext="mp4",
                    duration=12.5,
                    start="2026-10-01",
                    end="2026-10-31",
                ),
            ]
        )
    )


def shas(entries):
    return [e.sha256 for e in entries]


def test_dates_are_inclusive():
    m = manifest()
    all_ = [i.sha256 for i in m.items]
    assert shas(active_entries(m.items, date(2026, 10, 13))) == [all_[0], all_[1], all_[3]]
    assert shas(active_entries(m.items, date(2026, 10, 14))) == [all_[0], all_[2], all_[3]]
    assert shas(active_entries(m.items, date(2026, 11, 1))) == [all_[0], all_[2]]


def test_video_entries_have_no_image_duration():
    entries = active_entries(manifest().items, date(2026, 10, 13))
    assert entries[-1].kind == "video" and entries[-1].duration is None
    assert entries[0].duration == 10


def test_item_expires_at_local_midnight_not_utc():
    m = manifest()
    # 23:30 UTC on 13 Oct is already 00:30 on 14 Oct in Lisbon (UTC+1 in summer time).
    now = datetime(2026, 10, 13, 23, 30, tzinfo=UTC)
    today = local_today(now, LISBON)
    assert today == date(2026, 10, 14)
    assert m.items[1].sha256 not in shas(active_entries(m.items, today))


def test_next_evaluation_wakes_after_midnight():
    # 23:58 local -> wake about 2 minutes later, before the 5-minute interval.
    now = datetime(2026, 10, 13, 22, 58, tzinfo=UTC)  # 23:58 Lisbon
    wait = seconds_until_next_evaluation(now, LISBON, 300)
    assert 120 <= wait <= 122
    assert local_today(now + timedelta(seconds=wait), LISBON) == date(2026, 10, 14)
    # Mid-day the regular interval applies.
    assert seconds_until_next_evaluation(datetime(2026, 10, 13, 12, tzinfo=UTC), LISBON, 300) == 300


def test_midnight_on_dst_change_day():
    # 25 Oct 2026: Lisbon goes from UTC+1 to UTC+0 at 02:00 local. Midnight 26 Oct is 00:00 UTC.
    now = datetime(2026, 10, 25, 23, 50, tzinfo=UTC)  # 23:50 local (UTC+0)
    wait = seconds_until_next_evaluation(now, LISBON, 3600)
    assert 600 <= wait <= 602
    assert local_today(now + timedelta(seconds=wait), LISBON) == date(2026, 10, 26)


def test_clock_before_build_date_is_invalid():
    guard = ClockGuard(120, build_date=date(2026, 10, 13))
    assert guard.problem(datetime(1970, 1, 1, tzinfo=UTC)) is not None
    assert guard.problem(datetime(2026, 10, 13, 9, tzinfo=UTC)) is None


def test_clock_skew_against_server():
    guard = ClockGuard(120, build_date=date(2026, 1, 1))
    local = datetime(2026, 10, 13, 12, tzinfo=UTC)
    guard.observe_server_time(local - timedelta(minutes=5), local)
    assert "differs" in guard.problem(local)
    guard.observe_server_time(local - timedelta(seconds=30), local)
    assert guard.problem(local) is None
    guard.observe_server_time(None, local)  # no Date header: keep last observation
    assert guard.problem(local) is None


def test_parse_http_date():
    assert parse_http_date("Tue, 13 Oct 2026 12:00:00 GMT") == datetime(
        2026, 10, 13, 12, tzinfo=UTC
    )
    assert parse_http_date("nonsense") is None
    assert parse_http_date(None) is None
