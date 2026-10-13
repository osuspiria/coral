from __future__ import annotations

import pytest
import signagectl
from coral_dashboard.auth import hash_token, verify_password
from coral_dashboard.db import connect


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    return path


def run(data_dir, *argv):
    return signagectl.main(["--data-dir", str(data_dir), "--env-file", "none.env", *argv])


def test_add_prints_token_once_and_stores_only_hash(data_dir, capsys):
    assert run(data_dir, "player", "add", "--label", "pombal", "--location", "Loja") == 0
    out = capsys.readouterr().out.splitlines()
    token = out[-1]
    assert len(token) >= 43  # 256 bits, url-safe base64
    conn = connect(data_dir / "coral.sqlite3")
    row = conn.execute("SELECT * FROM players").fetchone()
    assert row["label"] == "pombal"
    assert row["token_hash"] == hash_token(token)
    assert token not in str(dict(row))


def test_add_rejects_duplicates_and_unknown_playlist(data_dir, capsys):
    assert run(data_dir, "player", "add", "--label", "pombal") == 0
    assert run(data_dir, "player", "add", "--label", "pombal") == 1
    assert run(data_dir, "player", "add", "--label", "other", "--playlist", "42") == 1
    assert "not found" in capsys.readouterr().err


def test_revoke(data_dir, capsys):
    run(data_dir, "player", "add", "--label", "pombal")
    assert run(data_dir, "player", "revoke", "1") == 0
    conn = connect(data_dir / "coral.sqlite3")
    assert conn.execute("SELECT revoked FROM players WHERE id = 1").fetchone()[0] == 1
    assert run(data_dir, "player", "revoke", "99") == 1


def test_hash_password(monkeypatch, capsys):
    answers = iter(["a long admin password", "a long admin password"])
    monkeypatch.setattr(signagectl.getpass, "getpass", lambda prompt: next(answers))
    assert signagectl.main(["--env-file", "none.env", "admin", "hash-password"]) == 0
    line = capsys.readouterr().out.splitlines()[-1]
    hashed = line.split("=", 1)[1].strip("'")
    assert verify_password(hashed, "a long admin password")
