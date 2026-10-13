from __future__ import annotations

import asyncio

from coral_player.health import MpvHealth
from coral_player.system import SystemActions, system_report


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_system_report(tmp_path):
    thermal = tmp_path / "temp"
    uptime = tmp_path / "uptime"
    thermal.write_text("51234\n")
    uptime.write_text("86400.52 12345.00\n")
    report = system_report(tmp_path, thermal, uptime)
    assert report["temp_c"] == 51.2
    assert report["uptime_s"] == 86400
    assert report["disk_free_mb"] > 0


def test_system_report_tolerates_missing_files(tmp_path):
    report = system_report(tmp_path / "missing", tmp_path / "x", tmp_path / "y")
    assert report == {"disk_free_mb": None, "temp_c": None, "uptime_s": None}


def test_actions_use_fixed_commands():
    calls = []

    async def runner(argv):
        calls.append(list(argv))
        return 0

    actions = SystemActions(runner)
    assert asyncio.run(actions.restart_mpv())
    assert asyncio.run(actions.reboot())
    assert calls == [
        ["systemctl", "restart", "--no-block", "mpv.service"],
        ["systemctl", "reboot"],
    ]


def test_failed_action_returns_false():
    async def runner(argv):
        return 1

    assert not asyncio.run(SystemActions(runner).reboot())


def test_stall_limit_respects_image_duration():
    clock = Clock()
    health = MpvHealth(clock)
    health.on_connect()
    health.observe(None, "a.png", idle=False)
    clock.t += 121
    assert health.is_stalled(None)
    assert health.is_stalled(10)
    assert not health.is_stalled(300)  # a 5-minute image is not a stall at 2 min
    clock.t += 300
    assert health.is_stalled(300)


def test_progress_resets_the_stall_timer():
    clock = Clock()
    health = MpvHealth(clock)
    health.on_connect()
    health.observe(1.0, "v.mp4", idle=False)
    clock.t += 100
    health.observe(2.0, "v.mp4", idle=False)  # time-pos moved
    clock.t += 100
    assert not health.is_stalled(None)
    clock.t += 30
    health.on_start_file()
    clock.t += 100
    assert not health.is_stalled(None)


def test_report_states():
    clock = Clock()
    health = MpvHealth(clock)
    assert health.report().state == "disconnected"
    health.on_connect()
    health.observe(None, None, idle=True)
    assert health.report().state == "idle"
    assert not health.is_stalled(None)  # idle is handled by reloading, not restarting
    health.observe(1.0, "v.mp4", idle=False)
    assert (health.report().state, health.report().progressing) == ("playing", True)
    health.mark_restarted()
    assert (health.report().state, health.report().restarts) == ("stuck", 1)
    health.observe(5.0, "v.mp4", idle=False)
    assert health.report().state == "playing"


def test_long_image_still_counts_as_progressing():
    clock = Clock()
    health = MpvHealth(clock)
    health.on_connect()
    health.observe(0.0, "img.png", idle=False)
    clock.t += 50
    assert not health.report().progressing
    assert health.report(image_duration_s=60).progressing
