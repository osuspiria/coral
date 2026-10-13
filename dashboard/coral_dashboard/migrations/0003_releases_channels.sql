-- Phase 3: signed player releases, update channels, per-player failed version.

CREATE TABLE releases (
    id         INTEGER PRIMARY KEY,
    version    TEXT    NOT NULL UNIQUE,
    sha256     TEXT    NOT NULL,
    size       INTEGER NOT NULL CHECK (size > 0),
    signature  TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE channels (
    name            TEXT PRIMARY KEY CHECK (name IN ('canary', 'stable')),
    desired_version TEXT REFERENCES releases (version)
);

INSERT INTO channels (name) VALUES ('canary'), ('stable');

-- Version that failed (rolled back or rejected) on this player; never requested
-- again until the desired version changes (spec 6.2.7).
ALTER TABLE players ADD COLUMN failed_version TEXT;
