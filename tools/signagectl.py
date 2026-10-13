"""signagectl: provision and revoke players, hash the admin password.

Run on the dashboard host, with the same environment (.env) as the dashboard:

    signagectl player add --label pombal [--location "Loja X"] [--playlist 3]
    signagectl player revoke 7
    signagectl admin hash-password
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path

from coral_dashboard.auth import hash_password, hash_token, new_player_token
from coral_dashboard.config import load_env_file
from coral_dashboard.db import connect, migrate
from coral_dashboard.manifest import ManifestContext
from coral_dashboard.repo import Conflict, NotFound, Store


def _store(args: argparse.Namespace) -> Store:
    data_dir = Path(args.data_dir or os.environ.get("CORAL_DATA_DIR", "./data")).resolve()
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
