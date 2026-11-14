#!/usr/bin/env sh
# Manual one-off backup. Use the opt-in scheduled backup service for retention.
set -eu
umask 077
target="${1:-backups/stockpilot.dump}"
mkdir -p "$(dirname "$target")"
if [ -e "$target" ] || [ -e "$target.sha256" ]; then
  echo "Refusing to overwrite an existing backup: $target" >&2
  exit 2
fi
partial="$target.partial.$$"
cleanup() { rm -f "$partial"; }
trap cleanup EXIT
docker compose exec -T db sh -ec '
  PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" \
    -d "$POSTGRES_DB" -Fc --no-owner --no-privileges
' > "$partial"
test -s "$partial"
docker compose exec -T db pg_restore --list < "$partial" > /dev/null
mv "$partial" "$target"
(
  cd "$(dirname "$target")"
  sha256sum "$(basename "$target")" > "$(basename "$target").sha256"
)
echo "Backup: $target (SHA-256 sidecar verified by restore command)"
