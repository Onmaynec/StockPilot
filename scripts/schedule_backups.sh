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
while :; do
  if /bin/sh /scripts/backup_rotate.sh; then
    echo "Backup successful; next run in $interval seconds"
    sleep "$interval"
  else
    echo "ERROR: backup failed; retrying after 300 seconds" >&2
    sleep 300
  fi
done
