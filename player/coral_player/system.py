"""System metrics for the heartbeat and the two privileged actions of the agent.

The agent runs without root: restarting mpv.service and rebooting go through
systemd/logind and are allowed by the polkit rule in deploy/pi (spec 5.2).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

THERMAL = Path("/sys/class/thermal/thermal_zone0/temp")
UPTIME = Path("/proc/uptime")


def system_report(data_dir: Path, thermal: Path = THERMAL, uptime: Path = UPTIME) -> dict[str, Any]:
    """Disk, temperature and uptime. A value that cannot be read is None."""
    report: dict[str, Any] = {"disk_free_mb": None, "temp_c": None, "uptime_s": None}
    with contextlib.suppress(OSError):
        report["disk_free_mb"] = shutil.disk_usage(data_dir).free // (1024 * 1024)
    with contextlib.suppress(OSError, ValueError):
        report["temp_c"] = round(int(thermal.read_text().strip()) / 1000, 1)
    with contextlib.suppress(OSError, ValueError, IndexError):
        report["uptime_s"] = int(float(uptime.read_text().split()[0]))
    return report


Runner = Callable[[Sequence[str]], Awaitable[int]]


async def run_command(argv: Sequence[str], timeout_s: float = 30) -> int:
    """Run a fixed command (never a shell). Returns the exit code, -1 on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout_s)
    except (OSError, TimeoutError) as exc:
        log.error("%s failed: %s", " ".join(argv), exc)
        return -1
    if proc.returncode:
        log.error(
            "%s exited with %s: %s",
            " ".join(argv),
            proc.returncode,
            stderr.decode(errors="replace").strip()[:300],
        )
    return proc.returncode or 0


class SystemActions:
    """restart-player and reboot. The argv lists are fixed; nothing comes from outside."""

    def __init__(self, runner: Runner = run_command) -> None:
        self._run = runner

    async def restart_mpv(self) -> bool:
        return await self._run(["systemctl", "restart", "--no-block", "mpv.service"]) == 0

    async def reboot(self) -> bool:
        return await self._run(["systemctl", "reboot"]) == 0
