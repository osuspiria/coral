"""SQLite connection handling and versioned SQL migrations."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


def now_iso() -> str:
    """Current UTC time as an ISO 8601 string (second precision)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect(path: Path | str) -> sqlite3.Connection:
    """Open a connection in WAL mode with foreign keys enforced.

    The connection runs in autocommit mode; use `transaction()` for atomic groups.
    """
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside BEGIN IMMEDIATE ... COMMIT, rolling back on error.

    Nested use joins the outer transaction.
    """
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _migrations() -> list[tuple[int, str, str]]:
    found = []
    for entry in resources.files("coral_dashboard.migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match:
            found.append((int(match.group(1)), entry.name, entry.read_text(encoding="utf-8")))
    found.sort()
    versions = [v for v, _, _ in found]
    if len(versions) != len(set(versions)):
        raise RuntimeError("Duplicate migration version")
    return found


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations in order. Returns the resulting schema version."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)"
    )
    current = conn.execute("SELECT COALESCE(MAX(version), 0) FROM schema_version").fetchone()[0]
    for version, name, sql in _migrations():
        if version <= current:
            continue
        # executescript() commits implicitly, so wrap the script in its own transaction.
        # `name` matched _MIGRATION_NAME, so interpolating it is safe.
        try:
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{sql}\n"
                f"INSERT INTO schema_version (version, name, applied_at) "  # noqa: S608
                f"VALUES ({version}, '{name}', '{now_iso()}');\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        current = version
    return current
