"""Data access layer. All SQL lives here and always uses bound parameters.

Every mutation that can change what a player sees refreshes the stored
`playlist_hash` inside the same transaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from .db import now_iso, transaction
from .manifest import ManifestContext, build_manifest


class NotFound(Exception):
    pass


class Conflict(Exception):
    pass


_ITEM_COLUMNS = """
    pi.id, pi.playlist_id, pi.media_id, pi.position, pi.start_date, pi.end_date,
    pi.image_duration_s, m.sha256, m.stored_name, m.original_name, m.kind, m.size,
    m.duration_s, m.width, m.height, m.deleted_at
"""


@dataclass
class Store:
    conn: sqlite3.Connection
    ctx: ManifestContext

    # ---------------------------------------------------------------- media

    def add_media(self, **fields: Any) -> int:
        """Insert a media row, or revive a soft-deleted one with the same sha256."""
        with transaction(self.conn):
            existing = self.conn.execute(
                "SELECT id FROM media WHERE sha256 = ?", (fields["sha256"],)
            ).fetchone()
            if existing:
                self.conn.execute(
                    "UPDATE media SET deleted_at = NULL WHERE id = ?", (existing["id"],)
                )
                return int(existing["id"])
            cur = self.conn.execute(
                "INSERT INTO media (sha256, stored_name, original_name, kind, size, duration_s,"
                " codec, width, height, created_at)"
                " VALUES (:sha256, :stored_name, :original_name, :kind, :size, :duration_s,"
                " :codec, :width, :height, :created_at)",
                {"created_at": now_iso(), **fields},
            )
            return int(cur.lastrowid)

    def get_media_by_sha(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM media WHERE sha256 = ?", (sha256,)).fetchone()

    def get_media(self, media_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        if row is None:
            raise NotFound(f"Media {media_id} not found")
        return row

    def list_media(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT m.*, (SELECT COUNT(*) FROM playlist_items pi WHERE pi.media_id = m.id)"
            " AS uses FROM media m WHERE m.deleted_at IS NULL"
            " ORDER BY m.created_at DESC, m.id DESC"
        ).fetchall()

    def delete_media(self, media_id: int) -> None:
        """Remove media from the library. Playlists that use it keep working; the
        physical file is collected later through `media_to_collect`."""
        self.get_media(media_id)
        self.conn.execute(
            "UPDATE media SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
            (now_iso(), media_id),
        )

    def media_to_collect(self, grace_days: int) -> list[sqlite3.Row]:
        """Deleted media past the grace period that no playlist references."""
        cutoff = (datetime.now(UTC) - timedelta(days=grace_days)).replace(microsecond=0)
        return self.conn.execute(
            "SELECT m.* FROM media m WHERE m.deleted_at IS NOT NULL AND m.deleted_at <= ?"
            " AND NOT EXISTS (SELECT 1 FROM playlist_items pi WHERE pi.media_id = m.id)",
            (cutoff.isoformat(),),
        ).fetchall()

    def purge_media_row(self, media_id: int) -> bool:
        cur = self.conn.execute(
            "DELETE FROM media WHERE id = ? AND deleted_at IS NOT NULL"
            " AND NOT EXISTS (SELECT 1 FROM playlist_items WHERE media_id = ?)",
            (media_id, media_id),
        )
        return cur.rowcount == 1

    # ------------------------------------------------------------ playlists

    def create_playlist(self, name: str) -> int:
        name = _clean_name(name)
        with transaction(self.conn):
            cur = self.conn.execute(
                "INSERT INTO playlists (name, created_at) VALUES (?, ?)", (name, now_iso())
            )
            playlist_id = int(cur.lastrowid)
            self._refresh_hash(playlist_id)
            return playlist_id

    def rename_playlist(self, playlist_id: int, name: str) -> None:
        name = _clean_name(name)
        self.get_playlist(playlist_id)
        self.conn.execute("UPDATE playlists SET name = ? WHERE id = ?", (name, playlist_id))

    def delete_playlist(self, playlist_id: int) -> None:
        with transaction(self.conn):
            self.get_playlist(playlist_id)
            players = self.players_using(playlist_id)
            if players:
                labels = ", ".join(p["label"] for p in players)
                raise Conflict(f"Playlist is assigned to: {labels}. Unassign it first.")
            # Revoked players may still point at it; detach them.
            self.conn.execute(
                "UPDATE players SET playlist_id = NULL WHERE playlist_id = ?", (playlist_id,)
            )
            self.conn.execute("DELETE FROM playlists WHERE id = ?", (playlist_id,))

    def get_playlist(self, playlist_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM playlists WHERE id = ?", (playlist_id,)).fetchone()
        if row is None:
            raise NotFound(f"Playlist {playlist_id} not found")
        return row

    def list_playlists(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT p.*,"
            " (SELECT COUNT(*) FROM playlist_items pi WHERE pi.playlist_id = p.id) AS item_count,"
            " (SELECT COUNT(*) FROM players pl WHERE pl.playlist_id = p.id AND pl.revoked = 0)"
            " AS player_count"
            " FROM playlists p ORDER BY p.name COLLATE NOCASE, p.id"
        ).fetchall()

    def players_using(self, playlist_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM players WHERE playlist_id = ? AND revoked = 0 ORDER BY label",
            (playlist_id,),
        ).fetchall()

    def list_items(self, playlist_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM playlist_items pi"  # noqa: S608 (constant columns)
            " JOIN media m ON m.id = pi.media_id"
            " WHERE pi.playlist_id = ? ORDER BY pi.position, pi.id",
            (playlist_id,),
        ).fetchall()

    def get_item(self, item_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM playlist_items WHERE id = ?", (item_id,)).fetchone()
        if row is None:
            raise NotFound(f"Playlist item {item_id} not found")
        return row

    def add_item(
        self,
        playlist_id: int,
        media_id: int,
        start_date: str | None = None,
        end_date: str | None = None,
        image_duration_s: int | None = None,
    ) -> int:
        _check_dates(start_date, end_date)
        _check_duration(image_duration_s)
        with transaction(self.conn):
            self.get_playlist(playlist_id)
            media = self.get_media(media_id)
            if media["deleted_at"] is not None:
                raise Conflict("Media was deleted from the library")
            position = self.conn.execute(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM playlist_items WHERE playlist_id = ?",
                (playlist_id,),
            ).fetchone()[0]
            cur = self.conn.execute(
                "INSERT INTO playlist_items"
                " (playlist_id, media_id, position, start_date, end_date, image_duration_s)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (playlist_id, media_id, position, start_date, end_date, image_duration_s),
            )
            self._refresh_hash(playlist_id)
            return int(cur.lastrowid)

    def update_item(
        self,
        item_id: int,
        start_date: str | None,
        end_date: str | None,
        image_duration_s: int | None,
    ) -> None:
        _check_dates(start_date, end_date)
        _check_duration(image_duration_s)
        with transaction(self.conn):
            item = self.get_item(item_id)
            self.conn.execute(
                "UPDATE playlist_items SET start_date = ?, end_date = ?, image_duration_s = ?"
                " WHERE id = ?",
                (start_date, end_date, image_duration_s, item_id),
            )
            self._refresh_hash(item["playlist_id"])

    def move_item(self, item_id: int, new_position: int) -> None:
        """Move an item to `new_position` (1-based, clamped) and renumber the rest."""
        with transaction(self.conn):
            item = self.get_item(item_id)
            ids = self._item_ids(item["playlist_id"])
            ids.remove(item_id)
            index = min(max(new_position, 1), len(ids) + 1) - 1
            ids.insert(index, item_id)
            self._renumber(ids)
            self._refresh_hash(item["playlist_id"])

    def remove_item(self, item_id: int) -> None:
        with transaction(self.conn):
            item = self.get_item(item_id)
            self.conn.execute("DELETE FROM playlist_items WHERE id = ?", (item_id,))
            self._renumber(self._item_ids(item["playlist_id"]))
            self._refresh_hash(item["playlist_id"])

    def _item_ids(self, playlist_id: int) -> list[int]:
        rows = self.conn.execute(
            "SELECT id FROM playlist_items WHERE playlist_id = ? ORDER BY position, id",
            (playlist_id,),
        )
        return [r["id"] for r in rows]

    def _renumber(self, ids: list[int]) -> None:
        self.conn.executemany(
            "UPDATE playlist_items SET position = ? WHERE id = ?",
            [(pos, item_id) for pos, item_id in enumerate(ids, start=1)],
        )

    def _refresh_hash(self, playlist_id: int) -> str:
        manifest = build_manifest(
            self.list_items(playlist_id), self.ctx, assigned=True, cleared=False
        )
        self.conn.execute(
            "UPDATE playlists SET playlist_hash = ? WHERE id = ?",
            (manifest["playlist_hash"], playlist_id),
        )
        return manifest["playlist_hash"]

    def refresh_all_hashes(self) -> None:
        """Recompute every stored hash (the manifest also depends on settings)."""
        with transaction(self.conn):
            for row in self.conn.execute("SELECT id FROM playlists").fetchall():
                self._refresh_hash(row["id"])

    # -------------------------------------------------------------- players

    def create_player(
        self, label: str, token_hash: str, location: str = "", playlist_id: int | None = None
    ) -> int:
        label = _clean_name(label)
        with transaction(self.conn):
            if playlist_id is not None:
                self.get_playlist(playlist_id)
            try:
                cur = self.conn.execute(
                    "INSERT INTO players (label, location, playlist_id, token_hash, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (label, location.strip(), playlist_id, token_hash, now_iso()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"A player labelled {label!r} already exists") from exc
            return int(cur.lastrowid)

    def get_player(self, player_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
        if row is None:
            raise NotFound(f"Player {player_id} not found")
        return row

    def get_active_player_by_token_hash(self, token_hash: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM players WHERE token_hash = ? AND revoked = 0", (token_hash,)
        ).fetchone()

    def list_players(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT pl.*, p.name AS playlist_name FROM players pl"
            " LEFT JOIN playlists p ON p.id = pl.playlist_id"
            " ORDER BY pl.revoked, pl.label COLLATE NOCASE"
        ).fetchall()

    def revoke_player(self, player_id: int) -> None:
        self.get_player(player_id)
        self.conn.execute("UPDATE players SET revoked = 1 WHERE id = ?", (player_id,))

    def assign_playlist(self, player_id: int, playlist_id: int) -> None:
        """Assign a playlist. Assigning always ends an explicit clear."""
        with transaction(self.conn):
            self.get_player(player_id)
            self.get_playlist(playlist_id)
            self.conn.execute(
                "UPDATE players SET playlist_id = ?, cleared = 0 WHERE id = ?",
                (playlist_id, player_id),
            )

    def unassign_playlist(self, player_id: int, *, clear: bool) -> None:
        """Remove the playlist. Without `clear` the player keeps its last list."""
        with transaction(self.conn):
            self.get_player(player_id)
            self.conn.execute(
                "UPDATE players SET playlist_id = NULL, cleared = ? WHERE id = ?",
                (1 if clear else 0, player_id),
            )

    def set_cleared(self, player_id: int, cleared: bool) -> None:
        """Explicit "Clear screen" (True) or "Resume" (False)."""
        self.get_player(player_id)
        self.conn.execute(
            "UPDATE players SET cleared = ? WHERE id = ?", (1 if cleared else 0, player_id)
        )

    def player_etag(self, player: sqlite3.Row) -> str:
        """Hash of the player's current manifest, without loading the items."""
        assigned = player["playlist_id"] is not None
        cleared = bool(player["cleared"])
        if assigned and not cleared:
            return self.get_playlist(player["playlist_id"])["playlist_hash"]
        return build_manifest([], self.ctx, assigned=assigned, cleared=cleared)["playlist_hash"]

    def player_manifest(self, player: sqlite3.Row) -> dict[str, Any]:
        assigned = player["playlist_id"] is not None
        cleared = bool(player["cleared"])
        rows = self.list_items(player["playlist_id"]) if assigned and not cleared else []
        return build_manifest(rows, self.ctx, assigned=assigned, cleared=cleared)

    def player_may_download(self, player: sqlite3.Row, sha256: str) -> bool:
        if player["playlist_id"] is None or player["cleared"]:
            return False
        row = self.conn.execute(
            "SELECT 1 FROM playlist_items pi JOIN media m ON m.id = pi.media_id"
            " WHERE pi.playlist_id = ? AND m.sha256 = ? LIMIT 1",
            (player["playlist_id"], sha256),
        ).fetchone()
        return row is not None


def _clean_name(name: str) -> str:
    name = name.strip()
    if not name or len(name) > 100:
        raise ValueError("Name must have between 1 and 100 characters")
    return name


def _check_dates(start_date: str | None, end_date: str | None) -> None:
    for value in (start_date, end_date):
        if value is None:
            continue
        try:
            parsed = datetime.strptime(value, "%Y-%m-%d")
        except ValueError as exc:
            raise ValueError(f"Invalid date {value!r}, expected YYYY-MM-DD") from exc
        if parsed.strftime("%Y-%m-%d") != value:
            raise ValueError(f"Invalid date {value!r}, expected YYYY-MM-DD")
    if start_date and end_date and start_date > end_date:
        raise ValueError("Start date must not be after end date")


def _check_duration(image_duration_s: int | None) -> None:
    if image_duration_s is not None and not (1 <= image_duration_s <= 86400):
        raise ValueError("Image duration must be between 1 and 86400 seconds")
