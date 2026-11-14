#!/usr/bin/env sh
# Rehearsal: restores a real pg_dump archive into a new temporary database.
# This script never drops or mutates the source database.
set -eu
if [ "${STOCKPILOT_VERIFY_RESTORE:-}" != "1" ]; then
  echo "Set STOCKPILOT_VERIFY_RESTORE=1 for an approved restore rehearsal." >&2
  exit 2
fi
verify_db="stockpilot_verify_$(date +%s)_$$"
case "$verify_db" in *[!a-zA-Z0-9_]*) echo "Invalid temporary database name" >&2; exit 2;; esac
archive="$(mktemp)"
created=0
cleanup() {
  if [ "$created" -eq 1 ]; then
    docker compose exec -T -e VERIFY_DB="$verify_db" db sh -ec '
      PGPASSWORD="$POSTGRES_PASSWORD" dropdb --if-exists --force -U "$POSTGRES_USER" "$VERIFY_DB"
    ' || echo "WARNING: remove temporary database $verify_db manually." >&2
  fi
  rm -f "$archive"
}
trap cleanup EXIT INT TERM
source_db="$(docker compose exec -T db sh -ec 'printf "%s" "$POSTGRES_DB"')"
test -n "$source_db"
sql() {
  docker compose exec -T -e VERIFY_DB="$1" -e VERIFY_SQL="$2" db sh -ec '
    PGPASSWORD="$POSTGRES_PASSWORD" psql -X -v ON_ERROR_STOP=1 \
      -U "$POSTGRES_USER" -d "$VERIFY_DB" -At -c "$VERIFY_SQL"
  '
}
# Consistent counts, ordered ledger and audit digests, and physical balances.
fingerprint="SELECT concat_ws('|',
  (SELECT count(*) FROM users),
  (SELECT count(*) FROM products),
  (SELECT count(*) FROM warehouses),
  (SELECT count(*) FROM stock_movements),
  (SELECT count(*) FROM audit_events),
  (SELECT count(*) FROM reservations),
  (SELECT count(*) FROM stock_blocks),
  (SELECT count(*) FROM orders),
  (SELECT count(*) FROM outbox_events),
  (SELECT md5(coalesce(string_agg(
      concat_ws(':',m.id,m.product_id,m.warehouse_id,m.bin_id,m.lot_id,
          m.qty,m.doc_type,m.doc_id,coalesce(m.actor_id::text,''),
          m.created_at::text),E'\n' ORDER BY m.id),''))
   FROM stock_movements m),
  (SELECT md5(coalesce(string_agg(
      concat_ws(':',a.id,a.action,a.entity_type,a.entity_id,a.actor_id,
          a.before::text,a.after::text,a.request_id,a.created_at::text),
       E'\n' ORDER BY a.id),''))
   FROM audit_events a),
  (SELECT md5(coalesce(string_agg(
      concat_ws(':',p.product_id,p.warehouse_id,p.bin_id,p.lot_id,p.qty),
      E'\n' ORDER BY p.product_id,p.warehouse_id,p.bin_id,p.lot_id),''))
   FROM (SELECT product_id,warehouse_id,bin_id,lot_id,sum(qty) AS qty
         FROM stock_movements GROUP BY product_id,warehouse_id,bin_id,lot_id) p)
)"
start="$(date +%s)"
source_before="$(sql "$source_db" "$fingerprint")"
docker compose exec -T db sh -ec '
  PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc
' > "$archive"
test -s "$archive"
dump_seconds=$(( $(date +%s) - start ))
source_after="$(sql "$source_db" "$fingerprint")"
if [ "$source_before" != "$source_after" ]; then
  echo "Source data changed while backing up; run again during a quiet period." >&2
  exit 1
fi
sql "$source_db" "CREATE DATABASE $verify_db" > /dev/null
created=1
restore_start="$(date +%s)"
docker compose exec -T -e VERIFY_DB="$verify_db" db sh -ec '
  PGPASSWORD="$POSTGRES_PASSWORD" pg_restore -U "$POSTGRES_USER" \
    -d "$VERIFY_DB" --exit-on-error --no-owner --no-privileges
' < "$archive"
restore_seconds=$(( $(date +%s) - restore_start ))
restored="$(sql "$verify_db" "$fingerprint")"
if [ "$source_after" != "$restored" ]; then
  echo "RESTORE MISMATCH: no changes were made to the source database." >&2
  exit 1
fi
printf '{"result":"passed","backupSeconds":%s,"restoreSeconds":%s,"totalSeconds":%s,"sha256Archive":"%s","databaseFingerprint":"%s","scope":"isolated database rehearsal; production RTO is unverified"}\n' \
  "$dump_seconds" "$restore_seconds" "$(( $(date +%s) - start ))" \
  "$(sha256sum "$archive" | cut -d' ' -f1)" \
  "$(printf "%s" "$restored" | sha256sum | cut -d' ' -f1)"
