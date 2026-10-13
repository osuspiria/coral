-- Phase 2: remote commands (closed list) and heartbeat lookups.

CREATE TABLE player_commands (
    id           INTEGER PRIMARY KEY,
    player_id    INTEGER NOT NULL REFERENCES players (id) ON DELETE CASCADE,
    name         TEXT    NOT NULL CHECK (name IN ('sync-now', 'restart-player', 'reboot')),
    created_at   TEXT    NOT NULL,
    delivered_at TEXT,
    acked_at     TEXT,
    expired_at   TEXT
);

CREATE INDEX player_commands_open ON player_commands (player_id)
    WHERE acked_at IS NULL AND expired_at IS NULL;

CREATE INDEX players_last_seen ON players (last_seen);
