"""Emergency access addressing (spec 6.4).

The VPS is 10.77.0.1/16; players get 10.77.x.y, assigned once and kept for the
player's lifetime. The Pi generates its own key pair; only the public key is
stored here.
"""

from __future__ import annotations

import base64
import binascii
import ipaddress
import sqlite3

NETWORK = ipaddress.ip_network("10.77.0.0/16")
SERVER_IP = ipaddress.ip_address("10.77.0.1")


class WireGuardError(Exception):
    pass


def check_public_key(key: str) -> str:
    key = key.strip()
    try:
        raw = base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise WireGuardError("not a WireGuard public key (base64)") from exc
    if len(raw) != 32 or len(key) != 44:
        raise WireGuardError("not a WireGuard public key (must be 32 bytes)")
    return key


def next_free_ip(used: set[str]) -> str:
    for address in NETWORK.hosts():
        if address == SERVER_IP or address.packed[-1] in (0, 255):
            continue
        if str(address) not in used:
            return str(address)
    raise WireGuardError("no free address left in 10.77.0.0/16")


def assign(conn: sqlite3.Connection, player_id: int, public_key: str) -> str:
    """Give the player a tunnel address (kept if it already has one)."""
    public_key = check_public_key(public_key)
    row = conn.execute("SELECT wg_ip FROM players WHERE id = ?", (player_id,)).fetchone()
    if row is None:
        raise WireGuardError(f"player {player_id} not found")
    address = row["wg_ip"]
    if address is None:
        used = {r[0] for r in conn.execute("SELECT wg_ip FROM players WHERE wg_ip IS NOT NULL")}
        address = next_free_ip(used)
    try:
        conn.execute(
            "UPDATE players SET wg_pubkey = ?, wg_ip = ? WHERE id = ?",
            (public_key, address, player_id),
        )
    except sqlite3.IntegrityError as exc:
        raise WireGuardError("this public key is already used by another player") from exc
    return address


def unassign(conn: sqlite3.Connection, player_id: int) -> str | None:
    """Forget the player's key (its address is kept for history). Returns the old key."""
    row = conn.execute("SELECT wg_pubkey FROM players WHERE id = ?", (player_id,)).fetchone()
    if row is None or row["wg_pubkey"] is None:
        return None
    conn.execute("UPDATE players SET wg_pubkey = NULL WHERE id = ?", (player_id,))
    return row["wg_pubkey"]


def vps_add_commands(public_key: str, address: str) -> list[str]:
    return [
        f"sudo wg set wg0 peer {public_key} allowed-ips {address}/32",
        "sudo wg-quick save wg0",
    ]


def vps_remove_commands(public_key: str) -> list[str]:
    return [f"sudo wg set wg0 peer {public_key} remove", "sudo wg-quick save wg0"]


def pi_command(address: str, endpoint: str, server_public_key: str) -> str:
    return (
        "sudo /opt/signage/current/deploy/pi/install.sh --wireguard-only"
        f" --wg-address {address} --wg-endpoint {endpoint}"
        f" --wg-server-pubkey {server_public_key}"
    )
