-- Phase 4: emergency access over WireGuard (spec 6.4). The Pi keeps its private
-- key; the server stores its public key and the tunnel address it was given.

ALTER TABLE players ADD COLUMN wg_pubkey TEXT;
ALTER TABLE players ADD COLUMN wg_ip TEXT;

CREATE UNIQUE INDEX players_wg_pubkey ON players (wg_pubkey) WHERE wg_pubkey IS NOT NULL;
CREATE UNIQUE INDEX players_wg_ip ON players (wg_ip) WHERE wg_ip IS NOT NULL;
