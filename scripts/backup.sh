#!/usr/bin/env sh
set -eu
target="${1:-backups/stockpilot.dump}"
mkdir -p "$(dirname "$target")"
docker compose exec -T db sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$target"
echo "Backup: $target"
