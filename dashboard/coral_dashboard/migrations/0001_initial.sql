-- Phase 1 schema. player_commands and alerts arrive with their phases.

CREATE TABLE media (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT    NOT NULL UNIQUE,
    stored_name   TEXT    NOT NULL,
    original_name TEXT    NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('image', 'video')),
    size          INTEGER NOT NULL CHECK (size > 0),
    duration_s    REAL,
    codec         TEXT,
    width         INTEGER,
    height        INTEGER,
    created_at    TEXT    NOT NULL,
    deleted_at    TEXT
);

CREATE TABLE playlists (
    id            INTEGER PRIMARY KEY,
    name          TEXT    NOT NULL,
    playlist_hash TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL
);

CREATE TABLE playlist_items (
    id               INTEGER PRIMARY KEY,
    playlist_id      INTEGER NOT NULL REFERENCES playlists (id) ON DELETE CASCADE,
    media_id         INTEGER NOT NULL REFERENCES media (id),
    position         INTEGER NOT NULL,
    start_date       TEXT,
    end_date         TEXT,
    image_duration_s INTEGER CHECK (image_duration_s IS NULL OR image_duration_s > 0),
    CHECK (start_date IS NULL OR end_date IS NULL OR start_date <= end_date)
);

CREATE INDEX playlist_items_by_playlist ON playlist_items (playlist_id, position);
CREATE INDEX playlist_items_by_media ON playlist_items (media_id);

CREATE TABLE players (
    id              INTEGER PRIMARY KEY,
    label           TEXT    NOT NULL UNIQUE,
    location        TEXT    NOT NULL DEFAULT '',
    playlist_id     INTEGER REFERENCES playlists (id) ON DELETE RESTRICT,
    cleared         INTEGER NOT NULL DEFAULT 0 CHECK (cleared IN (0, 1)),
    channel         TEXT    NOT NULL DEFAULT 'stable' CHECK (channel IN ('canary', 'stable')),
    desired_version TEXT,
    token_hash      TEXT    NOT NULL UNIQUE,
    revoked         INTEGER NOT NULL DEFAULT 0 CHECK (revoked IN (0, 1)),
    last_seen       TEXT,
    last_state      TEXT,
    created_at      TEXT    NOT NULL
);
