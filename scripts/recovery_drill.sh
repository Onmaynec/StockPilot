#!/bin/sh
# Boot a complete isolated StockPilot stack from a verified pg_dump archive.
# Never starts a recovery container on the live project's network or PGDATA.
set -eu
umask 077
if [ "${STOCKPILOT_RECOVERY_DRILL:-}" != "1" ] ||
   [ "${STOCKPILOT_RECOVERY_ISOLATED:-}" != "1" ]; then
  echo "Requires STOCKPILOT_RECOVERY_DRILL=1 and STOCKPILOT_RECOVERY_ISOLATED=1" >&2
  exit 2
fi
archive="${1:?Usage: STOCKPILOT_RECOVERY_DRILL=1 STOCKPILOT_RECOVERY_ISOLATED=1 sh scripts/recovery_drill.sh backups/archive.dump}"
test -s "$archive"
test -s "$archive.sha256"
dirname_archive="$(dirname "$archive")"
basename_archive="$(basename "$archive")"
(cd "$dirname_archive" && sha256sum --check "$basename_archive.sha256" >/dev/null)

project="stockpilot_drill_$(date +%s)_$$"
case "$project" in *[!a-z0-9_]*) echo "Invalid drill project name" >&2; exit 2;; esac
cleanup() {
  docker compose -p "$project" -f compose.recovery.yml down --volumes --remove-orphans >/dev/null 2>&1 ||
    echo "WARNING: isolated recovery project $project needs manual cleanup" >&2
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

live_fingerprint() {
  docker compose exec -T db sh -ec '
    PGPASSWORD="$POSTGRES_PASSWORD" psql -X -v ON_ERROR_STOP=1 -At \
      -U "$POSTGRES_USER" -d "$POSTGRES_DB"
  ' < scripts/recovery_fingerprint.sql
}
restore_fingerprint() {
  docker compose -p "$project" -f compose.recovery.yml exec -T db sh -ec '
    PGPASSWORD="$POSTGRES_PASSWORD" psql -X -v ON_ERROR_STOP=1 -At \
      -U "$POSTGRES_USER" -d "$POSTGRES_DB"
  ' < scripts/recovery_fingerprint.sql
}

# On this rehearsal, source is expected to be quiet after snapshot creation.
source_before="$(live_fingerprint)"
start="$(date +%s)"
docker compose -p "$project" -f compose.recovery.yml up -d --wait --wait-timeout 120 db >/dev/null
restore_start="$(date +%s)"
docker compose -p "$project" -f compose.recovery.yml exec -T db sh -ec '
  PGPASSWORD="$POSTGRES_PASSWORD" pg_restore --exit-on-error --no-owner \
    --no-privileges -U "$POSTGRES_USER" -d "$POSTGRES_DB"
' < "$archive"
restore_seconds="$(( $(date +%s) - restore_start ))"

# Migrations recheck grants and bootstrap the app's restricted runtime role.
docker compose -p "$project" -f compose.recovery.yml up \
  -d --build --wait --wait-timeout 180 api worker web >/dev/null
docker compose -p "$project" -f compose.recovery.yml exec -T api python -m app.security_check >/dev/null
docker compose -p "$project" -f compose.recovery.yml exec -T web \
  wget -q -O /dev/null http://127.0.0.1/health/ready
docker compose -p "$project" -f compose.recovery.yml exec -T web \
  wget -q -O /dev/null http://127.0.0.1/

restored="$(restore_fingerprint)"
source_after="$(live_fingerprint)"
if [ "$source_before" != "$source_after" ]; then
  echo "Live source changed during rehearsal: cannot compare against this archive." >&2
  exit 1
fi
if [ "$source_after" != "$restored" ]; then
  echo "RESTORE MISMATCH: live source untouched; isolated recovery differs." >&2
  exit 1
fi

elapsed="$(( $(date +%s) - start ))"
printf '{"status":"passed","scope":"isolated-recovery;not-production-RTO","restoreSeconds":%s,"fullStackRecoverySeconds":%s,"apiReady":true,"webReady":true,"workerStarted":true,"fingerprintsMatch":true,"archiveSha256":"%s","fingerprintSha256":"%s"}\n' \
  "$restore_seconds" "$elapsed" \
  "$(sha256sum "$archive" | cut -d ' ' -f1)" \
  "$(printf '%s' "$restored" | sha256sum | cut -d ' ' -f1)"
