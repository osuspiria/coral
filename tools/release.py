"""Build and sign a player release (spec 6).

Run on the development machine (WSL), where the minisign secret key lives:

    python3 tools/release.py 0.3.0 --key ~/.minisign/coral.key

Produces dist/coral-player-<version>.tar.gz and .tar.gz.minisig. The trusted
comment of the signature is "coral-player <version>"; the updater refuses a
release whose trusted comment does not match the version it asked for.

--test-broken builds a deliberately broken release for the rollback acceptance
test: the agent exits at start, so it never commits and the updater rolls back.
"""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import io
import re
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
PATHS = ("player", "deploy/pi")
# Tests stay in the release: the Pi guide runs player/tests/test_mpv_real.py.
EXCLUDE = re.compile(r"(^|/)__pycache__(/|$)|\.pyc$")

BROKEN_MAIN = '''"""DELIBERATELY BROKEN TEST RELEASE (tools/release.py --test-broken).

The agent exits at start, so the update is never committed and the updater
must roll back automatically (phase 3 acceptance test). Never deploy this.
"""

import sys


def main(argv=None):
    print("coral-player: deliberately broken test release, exiting", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
'''


class ReleaseError(Exception):
    pass


def trusted_comment(version: str) -> str:
    return f"coral-player {version}"


def _git(*args: str, cwd: Path) -> bytes:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True).stdout


def repo_version(root: Path) -> str:
    text = (root / "player/coral_player/__init__.py").read_text(encoding="utf-8")
    match = re.search(r'__version__ = "([^"]+)"', text)
    if not match:
        raise ReleaseError("__version__ not found in player/coral_player/__init__.py")
    return match.group(1)


def build_tarball(
    root: Path,
    version: str,
    out_dir: Path,
    *,
    ref: str = "HEAD",
    broken: bool = False,
    build_date: dt.date | None = None,
) -> Path:
    """Archive `ref` (player/ and deploy/pi/ only), stamp the build
    metadata and write a reproducible tar.gz."""
    if not VERSION_RE.fullmatch(version):
        raise ReleaseError(f"invalid version {version!r}, expected MAJOR.MINOR.PATCH")
    archive = io.BytesIO(_git("archive", ref, *PATHS, cwd=root))
    build_date = build_date or dt.date.today()
    stamped = {
        "player/coral_player/_build.py": (
            '"""Build metadata, stamped by tools/release.py."""\n\n'
            f'VERSION = "{version}"\n'
            "# Any system time before this date is certainly wrong (spec 4.5, invalid clock).\n"
            f'BUILD_DATE = "{build_date.isoformat()}"\n'
            f"TEST_BROKEN = {broken}\n"
        ),
    }
    if broken:
        stamped["player/coral_player/__main__.py"] = BROKEN_MAIN

    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"coral-player-{version}.tar.gz"
    buffer = io.BytesIO()
    with (
        tarfile.open(fileobj=archive) as source,
        tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as tar,
    ):
        members = sorted(source.getmembers(), key=lambda m: m.name)
        for member in members:
            if EXCLUDE.search(member.name) or not (member.isfile() or member.isdir()):
                continue
            data = None
            if member.isfile():
                data = source.extractfile(member).read()
                if member.name == "player/coral_player/__init__.py":
                    data = re.sub(
                        rb'__version__ = "[^"]+"', f'__version__ = "{version}"'.encode(), data
                    )
                if member.name in stamped:
                    data = stamped[member.name].encode()
            info = tarfile.TarInfo(member.name)
            info.mode = 0o755 if member.isdir() else 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = "root"
            if member.isdir():
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    with target.open("wb") as fh, gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
        gz.write(buffer.getvalue())
    return target


def sign(tarball: Path, key: Path, version: str) -> Path:
    signature = tarball.with_name(tarball.name + ".minisig")
    subprocess.run(
        [
            "minisign",
            "-S",
            "-s",
            str(key),
            "-m",
            str(tarball),
            "-x",
            str(signature),
            "-t",
            trusted_comment(version),
        ],
        check=True,
    )
    return signature


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release.py", description=__doc__.splitlines()[0])
    parser.add_argument("version")
    parser.add_argument("--key", type=Path, required=True, help="minisign secret key")
    parser.add_argument("--out", type=Path, default=ROOT / "dist")
    parser.add_argument(
        "--test-broken", action="store_true", help="build a broken release for the rollback test"
    )
    args = parser.parse_args(argv)
    try:
        if _git("status", "--porcelain", cwd=ROOT).strip():
            raise ReleaseError("the git tree has uncommitted changes; commit them first")
        if not args.test_broken and repo_version(ROOT) != args.version:
            raise ReleaseError(
                f"player/coral_player/__init__.py says {repo_version(ROOT)}, not {args.version}"
            )
        if (args.out / f"coral-player-{args.version}.tar.gz").exists():
            raise ReleaseError(f"dist already has release {args.version}")
        tarball = build_tarball(ROOT, args.version, args.out, broken=args.test_broken)
        signature = sign(tarball, args.key, args.version)
    except (ReleaseError, subprocess.CalledProcessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    print(f"{tarball}\n{signature}\nsha256 {digest}")
    if args.test_broken:
        print("WARNING: deliberately broken test release; only for the rollback test.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
