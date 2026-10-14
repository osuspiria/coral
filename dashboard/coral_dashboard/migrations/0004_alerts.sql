-- Phase 4: alerts (spec 3.2, 3.4). One open alert per (player, kind).

CREATE TABLE alerts (
    id                  INTEGER PRIMARY KEY,
    player_id           INTEGER NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    kind                TEXT    NOT NULL,
    detail              TEXT    NOT NULL DEFAULT '',
    opened_at           TEXT    NOT NULL,
    resolved_at         TEXT,
    notified_at         TEXT,
    resolve_notified_at TEXT
);

CREATE UNIQUE INDEX alerts_one_open ON alerts (player_id, kind) WHERE resolved_at IS NULL;
CREATE INDEX alerts_by_time ON alerts (opened_at);

-- Consecutive heartbeats reporting mpv not playing (alert after 2).
ALTER TABLE players ADD COLUMN mpv_bad_streak INTEGER NOT NULL DEFAULT 0;
