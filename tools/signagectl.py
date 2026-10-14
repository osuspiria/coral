"""signagectl: provision and revoke players, add releases, hash the admin password.

Run on the dashboard host, with the same environment (.env) as the dashboard:

    signagectl player add --label pombal [--location "Loja X"] [--playlist 3]
    signagectl player revoke 7
    signagectl release add coral-player-0.3.0.tar.gz coral-player-0.3.0.tar.gz.minisig
    signagectl backup snapshot --out /var/lib/coral/backup/coral.sqlite3
    signagectl verify-data --data-dir /tmp/restore/var/lib/coral
    signagectl admin hash-password
"""

from __future__ import annotations

import argparse
import getpass
import hashlib
import os
import re
import shutil
import sqlite3
import sys
from pathlib import Path

from coral_dashboard import wireguard
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
    old_key = wireguard.unassign(store.conn, args.id)
    if old_key:
        print("Remove its emergency access on the VPS:")
        for command in wireguard.vps_remove_commands(old_key):
            print(f"  {command}")
    return 0


def cmd_player_wg_add(args: argparse.Namespace) -> int:
    """Register the Pi's WireGuard public key and print the commands for both ends."""
    endpoint = os.environ.get("CORAL_WG_ENDPOINT", "")
    server_key = os.environ.get("CORAL_WG_SERVER_PUBKEY", "")
    if not endpoint or not server_key:
        print(
            "error: set CORAL_WG_ENDPOINT (host:51820) and CORAL_WG_SERVER_PUBKEY "
            "in the dashboard .env",
            file=sys.stderr,
        )
        return 1
    store = _store(args)
    try:
        player = store.get_player(args.id)
        if player["revoked"]:
            raise wireguard.WireGuardError("the player is revoked")
        address = wireguard.assign(store.conn, args.id, args.pubkey)
    except (NotFound, wireguard.WireGuardError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Player {args.id} ({player['label']}) has tunnel address {address}.")
    print("1. On the VPS:")
    for command in wireguard.vps_add_commands(args.pubkey.strip(), address):
        print(f"  {command}")
    print("2. On the Pi:")
    print(f"  {wireguard.pi_command(address, endpoint, server_key)}")
    print(f"Then, from your machine: ssh -J <admin>@<vps> <user>@{address}")
    return 0


def cmd_player_wg_remove(args: argparse.Namespace) -> int:
    store = _store(args)
    old_key = wireguard.unassign(store.conn, args.id)
    if old_key is None:
        print("error: this player has no emergency access configured", file=sys.stderr)
        return 1
    print("On the VPS:")
    for command in wireguard.vps_remove_commands(old_key):
        print(f"  {command}")
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


def cmd_backup_snapshot(args: argparse.Namespace) -> int:
    """Consistent copy of the live database (SQLite online backup API), for restic."""
    source = _data_dir(args) / "coral.sqlite3"
    if not source.is_file():
        print(f"error: {source} not found", file=sys.stderr)
        return 1
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.unlink(missing_ok=True)
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(tmp)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    os.replace(tmp, out)
    print(f"Snapshot written to {out}")
    return 0


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_verify_data(args: argparse.Namespace) -> int:
    """Check a data directory (e.g. a restored backup): database integrity and that
    every file the database needs exists with the right size and sha256."""
    data_dir = Path(args.verify_dir).resolve()
    db = data_dir / "coral.sqlite3"
    problems: list[str] = []
    if not db.is_file():
        print(f"FAIL database {db} not found")
        return 1
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            problems.append(f"database integrity: {result}")
        media = conn.execute(
            "SELECT m.* FROM media m WHERE m.deleted_at IS NULL OR EXISTS"
            " (SELECT 1 FROM playlist_items pi WHERE pi.media_id = m.id)"
        ).fetchall()
        releases = conn.execute("SELECT * FROM releases").fetchall()
        counts = {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: S608
            for t in ("players", "playlists", "playlist_items", "media", "releases")
        }
    except sqlite3.DatabaseError as exc:
        print(f"FAIL database unreadable: {exc}")
        return 1
    finally:
        conn.close()
    checks = [(data_dir / "media" / m["stored_name"], m["size"], m["sha256"]) for m in media]
    checks += [
        (data_dir / "releases" / f"coral-player-{r['version']}.tar.gz", r["size"], r["sha256"])
        for r in releases
    ]
    for path, size, sha in checks:
        if not path.is_file():
            problems.append(f"missing {path.relative_to(data_dir)}")
        elif path.stat().st_size != size:
            problems.append(f"wrong size {path.relative_to(data_dir)}")
        elif _file_sha256(path) != sha:
            problems.append(f"sha256 mismatch {path.relative_to(data_dir)}")
    print("Rows: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
    print(f"Files checked: {len(checks)}")
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        return 1
    print("OK   data directory is complete and consistent")
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
    wg_add = player.add_parser("wg-add", help="register the Pi's WireGuard key (emergency access)")
    wg_add.add_argument("id", type=int)
    wg_add.add_argument("--pubkey", required=True, help="output of `wg pubkey` on the Pi")
    wg_add.set_defaults(func=cmd_player_wg_add)
    wg_remove = player.add_parser("wg-remove", help="remove the player's emergency access")
    wg_remove.add_argument("id", type=int)
    wg_remove.set_defaults(func=cmd_player_wg_remove)

    release = sub.add_parser("release", help="player releases").add_subparsers(
        dest="action", required=True
    )
    radd = release.add_parser("add", help="store a signed release built by tools/release.py")
    radd.add_argument("tarball")
    radd.add_argument("signature")
    radd.set_defaults(func=cmd_release_add)

    backup = sub.add_parser("backup", help="backups").add_subparsers(dest="action", required=True)
    snap = backup.add_parser("snapshot", help="consistent copy of the database for restic")
    snap.add_argument("--out", required=True)
    snap.set_defaults(func=cmd_backup_snapshot)
    verify = sub.add_parser("verify-data", help="check a (restored) data directory")
    verify.add_argument("--data-dir", dest="verify_dir", required=True)
    verify.set_defaults(func=cmd_verify_data)

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
