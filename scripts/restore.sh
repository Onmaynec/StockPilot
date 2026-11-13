#!/usr/bin/env sh
set -eu
source_file="${1:?Usage: scripts/restore.sh backups/stockpilot.dump}"
test -f "$source_file"
echo "RESTORE WILL REPLACE DATA IN THE TARGET DATABASE."
echo "Stop API and worker before restoring; continue only if you have a backup."
docker compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --clean --if-exists --no-owner --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < "$source_file"
echo "Restore finished. Check movements, reservations and audit counts."
