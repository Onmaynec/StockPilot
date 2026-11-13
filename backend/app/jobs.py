"""Idempotent reservation expiry worker and local REST integration emulator."""
import json
import logging
import os
import time

from .db import all_rows, audit, lock_stock, one, outbox, pool

log = logging.getLogger(__name__)


def expire_once(limit=100):
    """Safe to invoke concurrently; orders and SKU guards serialize expiration."""
    count = 0
    with pool.connection() as c:
        order_ids = all_rows(c, """
          SELECT DISTINCT l.order_id FROM reservations r
          JOIN order_lines l ON l.id=r.order_line_id
          WHERE r.status='active' AND r.expires_at<=now()
          ORDER BY l.order_id LIMIT %s
        """, (limit,))
    for record in order_ids:
        with pool.connection() as c, c.transaction():
            order = one(c, "SELECT * FROM orders WHERE id=%s FOR UPDATE SKIP LOCKED",
                        (record["order_id"],))
            if not order:
                continue
            active = all_rows(c, """
              SELECT r.* FROM reservations r
              JOIN order_lines l ON l.id=r.order_line_id
              WHERE l.order_id=%s AND r.status='active'
            """, (order["id"],))
            if not active or not any(r["expires_at"].timestamp() <= time.time() for r in active):
                continue
            lock_stock(c, [(r["product_id"], r["warehouse_id"]) for r in active])
            expired = [r for r in active if r["expires_at"].timestamp() <= time.time()]
            for row in expired:
                updated = c.execute("""
                  UPDATE reservations SET status='expired'
                  WHERE id=%s AND status='active' RETURNING id
                """, (row["id"],)).fetchone()
                if not updated:
                    continue
                c.execute("""
                  UPDATE pick_tasks SET status='cancelled'
                  WHERE reservation_id=%s AND status NOT IN ('cancelled')
                """, (row["id"],))
                audit(c, None, "reservation.expire", "reservation", row["id"],
                      before={"status": "active"}, after={"status": "expired"},
                      warehouse=row["warehouse_id"])
                outbox(c, "reservation.expired", row["id"], {"id": str(row["id"])})
                count += 1
            # No shipments posted: cancel order and release the OTHER holds, too.
            posted = one(c, """
              SELECT 1 FROM shipments WHERE order_id=%s AND status='posted'
            """, (order["id"],))
            if not posted:
                c.execute("""
                  UPDATE reservations SET status='released'
                  WHERE order_line_id IN (SELECT id FROM order_lines WHERE order_id=%s)
                  AND status='active'
                """, (order["id"],))
                c.execute("""
                  UPDATE pick_tasks SET status='cancelled'
                  WHERE order_id=%s AND status NOT IN ('cancelled')
                """, (order["id"],))
                c.execute("""
                  UPDATE orders SET status='cancelled',version=version+1,updated_at=now()
                  WHERE id=%s AND status NOT IN ('cancelled','shipped')
                """, (order["id"],))
                audit(c, None, "order.expire", "order", order["id"],
                      after={"status": "cancelled"})
    return count


def dispatch_outbox_once(limit=100):
    """Emulate the external REST integration without claiming actual delivery."""
    with pool.connection() as c, c.transaction():
        rows = all_rows(c, """
          SELECT id,topic,aggregate_id,payload
          FROM outbox_events WHERE published_at IS NULL
          ORDER BY id FOR UPDATE SKIP LOCKED LIMIT %s
        """, (limit,))
        for row in rows:
            log.info(json.dumps({"mockRest": True, "topic": row["topic"],
                                 "aggregateId": row["aggregate_id"], "payload": row["payload"]}))
            c.execute("UPDATE outbox_events SET published_at=now() WHERE id=%s", (row["id"],))
        return len(rows)


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    pool.open(wait=True)
    try:
        while True:
            try:
                expired = expire_once()
                dispatched = dispatch_outbox_once()
                if expired or dispatched:
                    log.info("expired=%s simulated_dispatch=%s", expired, dispatched)
            except Exception:
                log.exception("Worker iteration failed; retrying")
            time.sleep(15)
    finally:
        pool.close()


if __name__ == "__main__":
    run()
