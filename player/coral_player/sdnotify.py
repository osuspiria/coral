"""Minimal sd_notify (READY/WATCHDOG/STATUS) without dependencies."""

from __future__ import annotations

import os
import socket


def notify(message: str) -> bool:
    """Send a notification to systemd. No-op (False) when not run by systemd."""
    address = os.environ.get("NOTIFY_SOCKET")
    if not address or not hasattr(socket, "AF_UNIX"):
        return False
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(message.encode())
    except OSError:
        return False
    return True
