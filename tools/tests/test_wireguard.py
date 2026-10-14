from __future__ import annotations

import base64

import pytest
import signagectl
from coral_dashboard import wireguard
from coral_dashboard.db import connect

KEY_A = base64.b64encode(b"a" * 32).decode()
KEY_B = base64.b64encode(b"b" * 32).decode()
SERVER = base64.b64encode(b"s" * 32).decode()


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    path = tmp_path / "data"
    path.mkdir()
    monkeypatch.setenv("CORAL_WG_ENDPOINT", "coral.example.com:51820")
    monkeypatch.setenv("CORAL_WG_SERVER_PUBKEY", SERVER)
    return path


def run(data_dir, *argv):
    return signagectl.main(["--data-dir", str(data_dir), "--env-file", "none.env", *argv])


def test_public_key_validation():
    assert wireguard.check_public_key(f" {KEY_A} ") == KEY_A
    for bad in ["", "not-base64!", base64.b64encode(b"short").decode()]:
        with pytest.raises(wireguard.WireGuardError):
            wireguard.check_public_key(bad)


def test_next_free_ip_skips_server_and_edges():
    assert wireguard.next_free_ip(set()) == "10.77.0.2"
    assert wireguard.next_free_ip({"10.77.0.2", "10.77.0.3"}) == "10.77.0.4"
    used = {f"10.77.0.{i}" for i in range(2, 255)}
    assert wireguard.next_free_ip(used) == "10.77.1.1"


def test_wg_add_assigns_stable_unique_addresses(data_dir, capsys):
    run(data_dir, "player", "add", "--label", "p0")
    run(data_dir, "player", "add", "--label", "p1")
    capsys.readouterr()
    assert run(data_dir, "player", "wg-add", "1", "--pubkey", KEY_A) == 0
    out = capsys.readouterr().out
    assert "10.77.0.2" in out
    assert f"sudo wg set wg0 peer {KEY_A} allowed-ips 10.77.0.2/32" in out
    assert f"--wg-server-pubkey {SERVER}" in out and "coral.example.com:51820" in out
    assert run(data_dir, "player", "wg-add", "2", "--pubkey", KEY_B) == 0
    assert "10.77.0.3" in capsys.readouterr().out
    # Same key on another player is refused; re-registering keeps the address.
    assert run(data_dir, "player", "wg-add", "2", "--pubkey", KEY_A) == 1
    assert run(data_dir, "player", "wg-add", "1", "--pubkey", KEY_A) == 0
    assert "10.77.0.2" in capsys.readouterr().out


def test_revoke_prints_peer_removal(data_dir, capsys):
    run(data_dir, "player", "add", "--label", "p0")
    run(data_dir, "player", "wg-add", "1", "--pubkey", KEY_A)
    capsys.readouterr()
    assert run(data_dir, "player", "revoke", "1") == 0
    out = capsys.readouterr().out
    assert f"sudo wg set wg0 peer {KEY_A} remove" in out
    row = connect(data_dir / "coral.sqlite3").execute("SELECT * FROM players").fetchone()
    assert row["wg_pubkey"] is None and row["wg_ip"] == "10.77.0.2"
    assert run(data_dir, "player", "wg-add", "1", "--pubkey", KEY_A) == 1  # revoked


def test_wg_add_requires_vps_settings(data_dir, monkeypatch, capsys):
    monkeypatch.delenv("CORAL_WG_SERVER_PUBKEY")
    run(data_dir, "player", "add", "--label", "p0")
    assert run(data_dir, "player", "wg-add", "1", "--pubkey", KEY_A) == 1
    assert "CORAL_WG_SERVER_PUBKEY" in capsys.readouterr().err


def test_wg_remove(data_dir, capsys):
    run(data_dir, "player", "add", "--label", "p0")
    assert run(data_dir, "player", "wg-remove", "1") == 1
    run(data_dir, "player", "wg-add", "1", "--pubkey", KEY_A)
    capsys.readouterr()
    assert run(data_dir, "player", "wg-remove", "1") == 0
    assert "remove" in capsys.readouterr().out
