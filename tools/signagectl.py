"""signagectl: provision and revoke players, add releases, hash the admin password.

Run on the dashboard host, with the same environment (.env) as the dashboard:

    signagectl player add --label pombal [--location "Loja X"] [--playlist 3]
    signagectl player revoke 7
    signagectl release add coral-player-0.3.0.tar.gz coral-player-0.3.0.tar.gz.minisig
    signagectl admin hash-password
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path

from coral_dashboard.auth import hash_password, hash_token, new_player_token
from coral_dashboard.config import load_env_file
from coral_dashboard.db import connect, migrate
from coral_dashboard.manifest import ManifestContext
from coral_dashboard.repo import Conflict, NotFound, Store


def _data_dir(args: argparse.Namespace) -> Path:
    return Path(args.data_dir or os.environ.get("CORAL_DATA_DIR", "./data")).resolve()


def _store(args: argparse.Namespace) -> Store:
    data_dir = _data_dir(args)
    if not data_dir.is_dir():
        raise SystemExit(f"Data directory {data_dir} does not exist")
    conn = connect(data_dir / "coral.sqlite3")
    migrate(conn)
    ctx = ManifestContext(
        os.environ.get("CORAL_TIMEZONE", "Europe/Lisbon"),
        int(os.environ.get("CORAL_DEFAULT_IMAGE_DURATION_S", "10")),
    )
    return Store(conn, ctx)


def cmd_player_add(args: argparse.Namespace) -> int:
    store = _store(args)
    token = new_player_token()
    try:
        player_id = store.create_player(
            args.label, hash_token(token), location=args.location, playlist_id=args.playlist
        )
    except (Conflict, NotFound, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Player created: id={player_id} label={args.label}")
    print("Token (shown only once; store it in /etc/signage/token on the player):")
    print(token)
    return 0


def cmd_player_revoke(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        player = store.get_player(args.id)
        store.revoke_player(args.id)
    except NotFound as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Player {args.id} ({player['label']}) revoked. Its token no longer works.")
    return 0


MAX_RELEASE_BYTES = 200 * 1024 * 1024
RELEASE_NAME = re.compile(r"coral-player-(\d+\.\d+\.\d+)\.tar\.gz")


def cmd_release_add(args: argparse.Namespace) -> int:
    """Store a signed release. The signature is checked by the players, not here;
    this only catches obvious mistakes (wrong file, wrong version, too big)."""
    tarball, signature = Path(args.tarball), Path(args.signature)
    match = RELEASE_NAME.fullmatch(tarball.name)
    if not match:
        print("error: the tarball must be named coral-player-X.Y.Z.tar.gz", file=sys.stderr)
        return 1
    version = match.group(1)
    if not tarball.is_file() or not signature.is_file():
        print("error: tarball or signature not found", file=sys.stderr)
        return 1
    size = tarball.stat().st_size
    if not 0 < size <= MAX_RELEASE_BYTES:
        print(f"error: unexpected tarball size {size}", file=sys.stderr)
        return 1
    with tarball.open("rb") as fh:
        if fh.read(2) != b"\x1f\x8b":
            print("error: the tarball is not gzip", file=sys.stderr)
            return 1
    sig_text = signature.read_text(encoding="utf-8")
    if f"trusted comment: coral-player {version}" not in sig_text or len(sig_text) > 4096:
        print(f"error: the signature is not a minisign signature for {version}", file=sys.stderr)
        return 1
    digest = hashlib.sha256(tarball.read_bytes()).hexdigest()
    store = _store(args)
    if store.get_release(version) is not None:
        print(f"error: release {version} already exists (releases are immutable)", file=sys.stderr)
        return 1
    releases_dir = (
        Path(store.conn.execute("PRAGMA database_list").fetchone()[2]).parent / "releases"
    )
    releases_dir.mkdir(exist_ok=True)
    target = releases_dir / tarball.name
    tmp = target.with_name(target.name + ".tmp")
    shutil.copyfile(tarball, tmp)
    os.replace(tmp, target)
    try:
        store.add_release(version, digest, size, sig_text)
    except (Conflict, ValueError) as exc:
        target.unlink(missing_ok=True)
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Release {version} added (sha256 {digest[:16]}…, {size} bytes).")
    print("Set it as desired version in the dashboard (Releases), canary first.")
    return 0


def cmd_admin_hash_password(_args: argparse.Namespace) -> int:
    password = getpass.getpass("New admin password: ")
    if len(password) < 12:
        print("error: use at least 12 characters", file=sys.stderr)
        return 1
    if getpass.getpass("Repeat: ") != password:
        print("error: passwords do not match", file=sys.stderr)
        return 1
    print("Put this line in the dashboard .env:")
    print(f"CORAL_ADMIN_PASSWORD_HASH='{hash_password(password)}'")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="signagectl", description=__doc__.splitlines()[0])
    parser.add_argument("--data-dir", help="dashboard data directory (default: $CORAL_DATA_DIR)")
    parser.add_argument("--env-file", default=".env", help="environment file (default: .env)")
    sub = parser.add_subparsers(dest="group", required=True)

    player = sub.add_parser("player", help="manage players").add_subparsers(
        dest="action", required=True
    )
    add = player.add_parser("add", help="create a player and print its token once")
    add.add_argument("--label", required=True)
    add.add_argument("--location", default="")
    add.add_argument("--playlist", type=int, default=None, help="playlist id to assign")
    add.set_defaults(func=cmd_player_add)
    revoke = player.add_parser("revoke", help="invalidate a player's token")
    revoke.add_argument("id", type=int)
    revoke.set_defaults(func=cmd_player_revoke)

    release = sub.add_parser("release", help="player releases").add_subparsers(
        dest="action", required=True
    )
    radd = release.add_parser("add", help="store a signed release built by tools/release.py")
    radd.add_argument("tarball")
    radd.add_argument("signature")
    radd.set_defaults(func=cmd_release_add)

    admin = sub.add_parser("admin", help="admin account").add_subparsers(
        dest="action", required=True
    )
    hpw = admin.add_parser("hash-password", help="print an argon2id hash for .env")
    hpw.set_defaults(func=cmd_admin_hash_password)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env_file(Path(args.env_file))
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
