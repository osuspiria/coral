"""Alert rules and the evaluation cycle (spec 3.4).

Every cycle: compute the findings of each active player, open an alert for each
new finding (at most one open per player and kind), resolve the alerts whose
condition is gone, then send the pending notifications (openings and
resolutions). A failed notification stays pending and is retried next cycle.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .config import Settings
from .notify import Message, Notifier
from .repo import Store

log = logging.getLogger(__name__)

TITLES = {
    "offline": "offline",
    "sync_failing": "sync failing",
    "update_failed": "software update failed",
    "disk_low": "disk almost full",
    "temp_high": "temperature high",
    "clock_drift": "clock wrong",
    "mpv_not_playing": "screen not playing",
}
HIGH_PRIORITY = {"offline", "mpv_not_playing", "disk_low", "temp_high"}


@dataclass(frozen=True)
class Finding:
    kind: str
    detail: str


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def evaluate_player(player: sqlite3.Row, now: datetime, settings: Settings) -> list[Finding]:
    """Conditions for one player. State-based rules only apply while online:
    an offline player has its own alert and a stale report says nothing new."""
    last_seen = _parse_time(player["last_seen"])
    if last_seen is None:
        return []  # never reported (not installed yet): nothing to say
    silent = now - last_seen
    if silent > timedelta(minutes=settings.alert_offline_min):
        return [Finding("offline", f"no heartbeat for {int(silent.total_seconds() // 60)} min")]
    try:
        state = json.loads(player["last_state"] or "{}")
    except ValueError:
        return []
    findings: list[Finding] = []

    sync = state.get("sync") or {}
    if sync.get("last_error"):
        last_ok = _parse_time(sync.get("last_ok"))
        window = timedelta(minutes=settings.alert_sync_failing_min)
        if last_ok is None or now - last_ok > window:
            findings.append(Finding("sync_failing", str(sync["last_error"])[:300]))

    update = state.get("update") or {}
    if update.get("state") in {"rolled_back", "rejected"}:
        findings.append(
            Finding(
                "update_failed",
                f"{update.get('state')} {update.get('version') or ''}: "
                f"{update.get('last_error') or ''}".strip(),
            )
        )

    system = state.get("system") or {}
    disk = system.get("disk_free_mb")
    if isinstance(disk, int | float) and disk < settings.alert_disk_min_mb:
        findings.append(Finding("disk_low", f"{disk} MB free"))
    temp = system.get("temp_c")
    if isinstance(temp, int | float) and temp > settings.alert_temp_max_c:
        findings.append(Finding("temp_high", f"{temp} °C"))

    player_time = _parse_time(state.get("player_time"))
    skew = (player_time - last_seen).total_seconds() if player_time else None
    if state.get("clock_drift") or (skew is not None and abs(skew) > settings.alert_clock_skew_s):
        detail = "reported by the player" if skew is None else f"{skew:+.0f} s vs the server"
        findings.append(Finding("clock_drift", detail))

    if player["mpv_bad_streak"] >= 2:
        mpv = state.get("mpv") or {}
        findings.append(Finding("mpv_not_playing", f"mpv {mpv.get('state', 'unknown')}"))
    return findings


def message_for(alert: sqlite3.Row, *, resolution: bool) -> Message:
    title = TITLES.get(alert["kind"], alert["kind"])
    where = alert["label"] + (f" ({alert['location']})" if alert["location"] else "")
    if resolution:
        return Message(
            f"RESOLVED {where}: {title}",
            f"Since {alert['opened_at']}.",
            "default",
            "white_check_mark",
        )
    priority = "high" if alert["kind"] in HIGH_PRIORITY else "default"
    return Message(f"{where}: {title}", alert["detail"] or title, priority, "warning")


def run_cycle(store: Store, settings: Settings, notifier: Notifier, now: datetime) -> None:
    for player in store.list_players():
        if player["revoked"]:
            store.resolve_all_alerts(player["id"], now)
            continue
        findings = {f.kind: f for f in evaluate_player(player, now, settings)}
        current = store.open_alerts(player["id"])
        for kind, finding in findings.items():
            if kind not in current and store.open_alert(player["id"], kind, finding.detail, now):
                log.warning("Alert opened: %s %s (%s)", player["label"], kind, finding.detail)
        for kind, alert in current.items():
            if kind not in findings:
                store.resolve_alert(alert["id"], now)
                log.info("Alert resolved: %s %s", player["label"], kind)

    for alert in store.alerts_to_notify():
        resolution = alert["notified_at"] is not None
        if not resolution and alert["resolved_at"] is not None:
            # Opened and gone before anyone was told: nothing to say.
            store.mark_alert_notified(alert["id"], now, resolution=False)
            store.mark_alert_notified(alert["id"], now, resolution=True)
            continue
        if not notifier.configured or notifier.send(message_for(alert, resolution=resolution)):
            store.mark_alert_notified(alert["id"], now, resolution=resolution)
