from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
import signagectl
from coral_dashboard.db import connect, migrate
from coral_dashboard.manifest import ManifestContext
from coral_dashboard.repo import Store

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def data_dir(tmp_path):
    """A small but complete dashboard data directory."""
    data = tmp_path / "data"
    (data / "media").mkdir(parents=True)
    (data / "releases").mkdir()
    conn = connect(data / "coral.sqlite3")
    migrate(conn)
    store = Store(conn, ManifestContext("Europe/Lisbon", 10))
    for n in range(3):
        content = f"media {n}".encode() * 100
        sha = hashlib.sha256(content).hexdigest()
        (data / "media" / f"{sha}.png").write_bytes(content)
        media_id = store.add_media(
            sha256=sha,
            stored_name=f"{sha}.png",
            original_name=f"{n}.png",
            kind="image",
            size=len(content),
            duration_s=None,
            codec="png",
            width=1,
            height=1,
        )
        if n == 0:
            playlist = store.create_playlist("Loja")
            store.add_item(playlist, media_id)
    tarball = b"\x1f\x8b release"
    (data / "releases" / "coral-player-0.3.4.tar.gz").write_bytes(tarball)
    store.add_release("0.3.4", hashlib.sha256(tarball).hexdigest(), len(tarball), "sig")
    store.create_player("pombal", "t" * 64)
    conn.close()
    return data


def run(*argv):
    return signagectl.main(["--env-file", "none.env", *argv])


def test_snapshot_is_a_consistent_copy(data_dir, tmp_path):
    out = tmp_path / "backup" / "coral.sqlite3"
    assert run("--data-dir", str(data_dir), "backup", "snapshot", "--out", str(out)) == 0
    copy = sqlite3.connect(out)
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("SELECT COUNT(*) FROM media").fetchone()[0] == 3
    assert copy.execute("SELECT label FROM players").fetchone()[0] == "pombal"
    copy.close()
    assert not out.with_name("coral.sqlite3.tmp").exists()


def test_verify_data_ok(data_dir, capsys):
    assert run("verify-data", "--data-dir", str(data_dir)) == 0
    out = capsys.readouterr().out
    assert "Files checked: 4" in out and "OK" in out


@pytest.mark.parametrize("damage", ["missing", "corrupt", "truncated", "release"])
def test_verify_data_detects_damaged_files(data_dir, capsys, damage):
    media = sorted((data_dir / "media").iterdir())[0]
    if damage == "missing":
        media.unlink()
    elif damage == "corrupt":
        media.write_bytes(b"X" + media.read_bytes()[1:])
    elif damage == "truncated":
        media.write_bytes(media.read_bytes()[:-1])
    else:
        (data_dir / "releases" / "coral-player-0.3.4.tar.gz").write_bytes(b"\x1f\x8b other!")
    assert run("verify-data", "--data-dir", str(data_dir)) == 1
    assert "FAIL" in capsys.readouterr().out


def test_verify_data_detects_a_broken_database(data_dir, capsys):
    db = data_dir / "coral.sqlite3"
    raw = bytearray(db.read_bytes())
    raw[200:4096] = b"\x00" * (4096 - 200)  # wipe part of the schema page
    db.write_bytes(bytes(raw))
    assert run("verify-data", "--data-dir", str(data_dir)) == 1
    assert "FAIL" in capsys.readouterr().out


@pytest.mark.skipif(
    os.name != "posix" or shutil.which("restic") is None, reason="needs restic (Linux)"
)
def test_backup_script_then_restore_then_verify(data_dir, tmp_path):
    """The real deploy/vps/backup.sh with restic: back up, restore elsewhere, verify."""
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "dashboard.env").write_text("")
    (etc / "restic.pass").write_text("test-password\n")
    repo = tmp_path / "repo"
    (etc / "backup.env").write_text(
        f"RESTIC_REPOSITORY={repo}\nRESTIC_PASSWORD_FILE={etc / 'restic.pass'}\n"
    )
    env = {
        **os.environ,
        "CORAL_BACKUP_ENV": str(etc / "backup.env"),
        "CORAL_DATA_DIR": str(data_dir),
        "CORAL_APP_DIR": str(ROOT),
        "CORAL_ETC_DIR": str(etc),
        "CORAL_PYTHON": sys.executable,
        "RESTIC_PASSWORD_FILE": str(etc / "restic.pass"),
    }
    subprocess.run(
        ["restic", "init", "--repo", str(repo)], env=env, check=True, capture_output=True
    )
    result = subprocess.run(
        ["bash", str(ROOT / "deploy/vps/backup.sh")], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr

    restore = tmp_path / "restore"
    subprocess.run(
        ["restic", "restore", "latest", "--repo", str(repo), "--target", str(restore)],
        env=env,
        check=True,
        capture_output=True,
    )
    restored = restore / str(data_dir).lstrip("/")
    # The snapshot is the database to restore (not the live file).
    shutil.copy(restored / "backup" / "coral.sqlite3", restored / "coral.sqlite3")
    assert run("verify-data", "--data-dir", str(restored)) == 0
