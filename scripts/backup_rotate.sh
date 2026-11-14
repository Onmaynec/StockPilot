#!/bin/sh
# Atomic custom-format backup; opt-in service, never mounts PostgreSQL PGDATA.
set -eu
umask 077
backup_dir="${BACKUP_DIR:-/backups}"
retention="${BACKUP_RETENTION_DAYS:-14}"
case "$retention" in
  ''|*[!0-9]*) echo "BACKUP_RETENTION_DAYS must be an integer" >&2; exit 2;;
esac
if [ "$retention" -lt 2 ] || [ "$retention" -gt 3650 ]; then
  echo "BACKUP_RETENTION_DAYS must be 2..3650" >&2; exit 2
fi
: "${PGHOST:?PGHOST missing}"
: "${PGUSER:?PGUSER missing}"
: "${PGPASSWORD:?PGPASSWORD missing}"
: "${PGDATABASE:?PGDATABASE missing}"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
cd "$backup_dir"
if ! mkdir .stockpilot-backup.lock 2>/dev/null; then
  echo "Backup already running or stale lock in $backup_dir" >&2
  exit 3
fi
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
# mktemp avoids reusing filenames when separate containers run within one second.
temporary="$(mktemp ".stockpilot-$stamp-XXXXXX.partial")"
file="${temporary#.}"
file="${file%.partial}.dump"
cleanup() {
  rm -f "$temporary" ".$file.sha256.partial"
  rmdir .stockpilot-backup.lock 2>/dev/null || true
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

started="$(date +%s)"
pg_dump --format=custom --file="$temporary" --no-owner --no-privileges
test -s "$temporary"
pg_restore --list "$temporary" >/dev/null
mv "$temporary" "$file"
sha256sum "$file" > ".$file.sha256.partial"
mv ".$file.sha256.partial" "$file.sha256"
sha256sum -c "$file.sha256" >/dev/null

# The newest two archives are never removed, regardless of retention.
latest_two="$(ls -1t stockpilot-*.dump | head -n 2)"
find . -maxdepth 1 -type f -name 'stockpilot-*.dump' -mtime "+$retention" |
while IFS= read -r old; do
  short="${old#./}"
  case "
$latest_two
" in *"
$short
"*) continue;; esac
  if [ -f "$short.sha256" ]; then
    rm -f -- "$short" "$short.sha256"
  fi
done

size="$(wc -c < "$file" | tr -d ' ')"
elapsed="$(( $(date +%s) - started ))"
printf '{"result":"created","archive":"%s","sha256":"%s","bytes":%s,"elapsedSeconds":%s,"retentionDays":%s}\n' \
  "$file" "$(sha256sum "$file" | cut -d ' ' -f1)" "$size" "$elapsed" "$retention"
