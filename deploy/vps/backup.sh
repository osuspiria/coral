#!/usr/bin/env bash
# Daily backup of the dashboard with restic (spec 3.5), to a Google Drive shared drive
# through rclone. Run by coral-backup.service as root. Configuration in /etc/coral/backup.env:
#   RESTIC_REPOSITORY=rclone:gdrive:coral          (remote "gdrive" in $RCLONE_CONFIG)
#   RCLONE_CONFIG=/etc/coral/rclone.conf           (mode 600; service account key file)
#   RESTIC_PASSWORD_FILE=/etc/coral/restic.pass    (mode 600; keep a copy OFF the VPS)
#   HEALTHCHECK_BACKUP_URL=https://hc-ping.com/...  (optional; pinged only on success)
set -euo pipefail

ENV_FILE=${CORAL_BACKUP_ENV:-/etc/coral/backup.env}
DATA=${CORAL_DATA_DIR:-/var/lib/coral}
APP=${CORAL_APP_DIR:-/opt/coral}
ETC=${CORAL_ETC_DIR:-/etc/coral}
PYTHON=${CORAL_PYTHON:-$APP/.venv/bin/python}

# shellcheck disable=SC1090
set -a; . "$ENV_FILE"; set +a
: "${RESTIC_REPOSITORY:?set RESTIC_REPOSITORY in $ENV_FILE}"
: "${RESTIC_PASSWORD_FILE:?set RESTIC_PASSWORD_FILE in $ENV_FILE}"

ping() {
    if [ -n "${HEALTHCHECK_BACKUP_URL:-}" ]; then
        curl -fsS -m 10 --retry 3 "${HEALTHCHECK_BACKUP_URL}$1" >/dev/null || true
    fi
}
trap 'ping /fail' ERR

echo "== snapshot of the database"
PYTHONPATH="$APP/dashboard:$APP/tools" "$PYTHON" -m signagectl \
    --data-dir "$DATA" --env-file "$ETC/dashboard.env" \
    backup snapshot --out "$DATA/backup/coral.sqlite3"

echo "== restic backup"
restic backup --tag coral --exclude "$DATA/media/.incoming" \
    "$DATA/backup/coral.sqlite3" "$DATA/media" "$DATA/releases" "$ETC"

echo "== retention"
restic forget --tag coral --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune

if [ "$(date +%u)" = "7" ]; then
    echo "== weekly check"
    restic check
fi

ping ""
echo "== backup finished"
