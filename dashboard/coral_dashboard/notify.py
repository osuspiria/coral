"""Outbound notifications: ntfy for alerts, healthchecks.io for "the dashboard is
alive" (spec 3.4). Stdlib only, short timeouts; failures are logged and retried
by the caller, never raised into the request path."""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

TIMEOUT_S = 10


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    priority: str = "default"  # ntfy: min, low, default, high, urgent
    tags: str = ""


class Notifier:
    def __init__(self, ntfy_url: str, ntfy_token: str = "") -> None:
        self.url = ntfy_url.strip()
        self.token = ntfy_token.strip()

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def send(self, message: Message) -> bool:
        if not self.configured:
            return False
        headers = {
            # HTTP headers must be latin-1: keep the title ASCII-safe.
            "Title": message.title.encode("ascii", "replace").decode(),
            "Priority": message.priority,
            "Content-Type": "text/plain; charset=utf-8",
        }
        if message.tags:
            headers["Tags"] = message.tags
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(  # noqa: S310 (URL from the admin's .env)
            self.url, data=message.body.encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:  # noqa: S310
                return 200 <= response.status < 300
        except (urllib.error.URLError, OSError) as exc:
            log.warning("ntfy notification failed: %s", exc)
            return False


def ping_healthcheck(url: str) -> bool:
    if not url:
        return False
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:  # noqa: S310
            return 200 <= response.status < 300
    except (urllib.error.URLError, OSError) as exc:
        log.warning("healthcheck ping failed: %s", exc)
        return False
