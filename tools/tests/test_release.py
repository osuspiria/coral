from __future__ import annotations

import datetime as dt
import gzip
import hashlib
import io
import tarfile

import pytest
import release
import signagectl
from coral_dashboard.db import connect

DATE = dt.date(2026, 10, 14)


def members(tarball):
    with tarfile.open(tarball) as tar:
        return {m.name: (tar.extractfile(m).read() if m.isfile() else None) for m in tar}


def test_build_stamps_version_and_build_date(tmp_path):
    tarball = release.build_tarball(release.ROOT, "9.8.7", tmp_path, build_date=DATE)
    assert tarball.name == "coral-player-9.8.7.tar.gz"
    files = members(tarball)
    build = files["player/coral_player/_build.py"].decode()
    assert 'VERSION = "9.8.7"' in build and 'BUILD_DATE = "2026-10-14"' in build
    assert "TEST_BROKEN = False" in build
    assert b'__version__ = "9.8.7"' in files["player/coral_player/__init__.py"]
    assert "player/assets/fallback.png" in files
    assert "deploy/pi/mpv.service" in files
    assert "player/tests/test_mpv_real.py" in files  # used by the Pi guide
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in files)
    assert not any(name.startswith(("dashboard/", "tools/", "docs/")) for name in files)


def test_build_is_reproducible(tmp_path):
    a = release.build_tarball(release.ROOT, "1.2.3", tmp_path / "a", build_date=DATE)
    b = release.build_tarball(release.ROOT, "1.2.3", tmp_path / "b", build_date=DATE)
    assert a.read_bytes() == b.read_bytes()


def test_broken_release_agent_exits(tmp_path):
    tarball = release.build_tarball(release.ROOT, "9.9.9", tmp_path, build_date=DATE, broken=True)
    files = members(tarball)
    assert b"deliberately broken" in files["player/coral_player/__main__.py"]
    assert "TEST_BROKEN = True" in files["player/coral_player/_build.py"].decode()


def test_invalid_version(tmp_path):
    with pytest.raises(release.ReleaseError):
        release.build_tarball(release.ROOT, "1.2", tmp_path)


# ------------------------------------------------------------ release add


@pytest.fixture
def data_dir(tmp_path):
    path = tmp_path / "data"
    path.mkdir()
    return path


def make_release(tmp_path, version="0.3.0", comment=None):
    tarball = tmp_path / f"coral-player-{version}.tar.gz"
    with gzip.GzipFile(fileobj=(buf := io.BytesIO()), mode="wb") as gz:
        gz.write(b"payload")
    tarball.write_bytes(buf.getvalue())
    sig = tmp_path / f"{tarball.name}.minisig"
    comment = comment or f"coral-player {version}"
    sig.write_text(f"untrusted comment: x\nRWQ\ntrusted comment: {comment}\nabc\n")
    return tarball, sig


def run(data_dir, *argv):
    return signagectl.main(["--data-dir", str(data_dir), "--env-file", "none.env", *argv])


def test_release_add(data_dir, tmp_path, capsys):
    tarball, sig = make_release(tmp_path)
    assert run(data_dir, "release", "add", str(tarball), str(sig)) == 0
    stored = data_dir / "releases" / tarball.name
    assert stored.read_bytes() == tarball.read_bytes()
    row = connect(data_dir / "coral.sqlite3").execute("SELECT * FROM releases").fetchone()
    assert row["version"] == "0.3.0"
    assert row["sha256"] == hashlib.sha256(tarball.read_bytes()).hexdigest()
    assert "trusted comment: coral-player 0.3.0" in row["signature"]
    # Releases are immutable.
    assert run(data_dir, "release", "add", str(tarball), str(sig)) == 1
    assert "already exists" in capsys.readouterr().err


def test_release_add_rejects_mismatched_signature(data_dir, tmp_path, capsys):
    tarball, sig = make_release(tmp_path, comment="coral-player 0.2.0")
    assert run(data_dir, "release", "add", str(tarball), str(sig)) == 1
    assert "not a minisign signature for 0.3.0" in capsys.readouterr().err


def test_release_add_rejects_bad_names_and_content(data_dir, tmp_path):
    tarball, sig = make_release(tmp_path)
    renamed = tmp_path / "player.tar.gz"
    renamed.write_bytes(tarball.read_bytes())
    assert run(data_dir, "release", "add", str(renamed), str(sig)) == 1
    tarball.write_bytes(b"not gzip")
    assert run(data_dir, "release", "add", str(tarball), str(sig)) == 1
