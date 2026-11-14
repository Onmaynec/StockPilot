#!/bin/sh
# Run all restart-safe schema migrations under the owner account, then configure app grants.
set -eu
if [ "$PGUSER" = "$APP_DB_USER" ]; then
  echo "Application and migration database users must be different." >&2
  exit 1
fi
psql -v ON_ERROR_STOP=1 -f /migrations/002_sessions.sql
psql -v ON_ERROR_STOP=1 -f /migrations/003_inventory.sql
psql -v ON_ERROR_STOP=1 -f /migrations/004_rate_limits.sql
psql -v ON_ERROR_STOP=1 -f /migrations/005_catalog_search.sql
psql -v ON_ERROR_STOP=1 -v app_user="$APP_DB_USER" -v app_password="$APP_DB_PASSWORD" -f /setup/db_roles.sql
