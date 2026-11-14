#!/bin/sh
# Run one backup immediately, then repeat every interval; retry failures.
set -eu
interval="${BACKUP_INTERVAL_SECONDS:-86400}"
case "$interval" in
  ''|*[!0-9]*) echo "BACKUP_INTERVAL_SECONDS must be an integer" >&2; exit 2;;
esac
if [ "$interval" -lt 3600 ] || [ "$interval" -gt 604800 ]; then
  echo "BACKUP_INTERVAL_SECONDS must be 3600..604800" >&2; exit 2
fi
backup_dir="${BACKUP_DIR:-/backups}"
write_failure_status() {
  mkdir -p "$backup_dir" 2>/dev/null || return 0
  temporary="$(mktemp "$backup_dir/.stockpilot-backup-status.XXXXXX" 2>/dev/null)" || return 0
  {
    printf 'status=failed\n'
    printf 'timestamp=%s\n' "$(date +%s)"
  } > "$temporary"
  mv "$temporary" "$backup_dir/.stockpilot-backup-status" 2>/dev/null || rm -f "$temporary"
}
while :; do
  if /bin/sh /scripts/backup_rotate.sh; then
    echo "Backup successful; next run in $interval seconds"
    sleep "$interval"
  else
    write_failure_status
    echo "ERROR: backup failed; status is unhealthy; retrying after 300 seconds" >&2
    sleep 300
  fi
done
