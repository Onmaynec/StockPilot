#!/usr/bin/env sh
# Deliberately destructive recovery of the current DB; never run unattended.
set -eu
umask 077
if [ "${STOCKPILOT_ALLOW_DESTRUCTIVE_RESTORE:-}" != "1" ]; then
  echo "RESTORE WILL REPLACE CURRENT DATABASE DATA." >&2
  echo "Stop API/worker/web; set STOCKPILOT_ALLOW_DESTRUCTIVE_RESTORE=1 explicitly." >&2
  exit 2
fi
source_file="${1:?Usage: STOCKPILOT_ALLOW_DESTRUCTIVE_RESTORE=1 sh scripts/restore.sh backups/stockpilot.dump}"
test -s "$source_file"
test -s "$source_file.sha256" || {
  echo "Checksum sidecar missing; refusing unsigned backup." >&2; exit 2;
}
(
  cd "$(dirname "$source_file")"
  sha256sum --check "$(basename "$source_file").sha256"
)
if docker compose ps --status running --services | grep -E '^(api|worker|web)$' > /dev/null; then
  echo "Refusing destructive restore while api, worker or web is running." >&2
  exit 2
fi
echo "Restoring checked archive into current DB. Existing data will be replaced." >&2
docker compose exec -T db sh -ec '
  PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --exit-on-error \
    --clean --if-exists --no-owner --no-privileges \
    -U "$POSTGRES_USER" -d "$POSTGRES_DB"
' < "$source_file"
echo "Restore completed; verify schema migrations, health endpoints, and ledger." >&2
