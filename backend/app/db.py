"""SQL transaction boundaries and append-only accounting primitives."""
import hashlib
import json
import os
from contextvars import ContextVar

from fastapi import HTTPException
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

request_id = ContextVar("request_id", default="-")
pool = ConnectionPool(
    conninfo=os.environ["DATABASE_URL"],
    min_size=1,
    max_size=12,
    kwargs={"row_factory": dict_row},
    open=False,
)


def one(c, query, args=()):
    return c.execute(query, args).fetchone()


def all_rows(c, query, args=()):
    return c.execute(query, args).fetchall()


def error(code, message, status=400, details=None):
    raise HTTPException(status_code=status, detail={
        "code": code, "message": message, "details": details,
    })


def audit(c, actor, action, entity_type, entity_id, before=None, after=None, warehouse=None):
    c.execute(
        """INSERT INTO audit_events
           (actor_id,action,entity_type,entity_id,warehouse_id,before,after,request_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
        (actor, action, entity_type, str(entity_id), warehouse,
         Jsonb(before) if before is not None else None,
         Jsonb(after) if after is not None else None, request_id.get()),
    )


def outbox(c, topic, entity_id, payload):
    c.execute(
        "INSERT INTO outbox_events(topic,aggregate_id,payload) VALUES(%s,%s,%s)",
        (topic, str(entity_id), Jsonb(payload)),
    )


def lock_stock(c, pairs):
    """Canonical lock order prevents deadlocks across multi-SKU operations."""
    for product_id, warehouse_id in sorted(
        {(str(p), str(w)) for p, w in pairs}
    ):
        c.execute(
            """INSERT INTO stock_guards(product_id,warehouse_id)
               VALUES (%s,%s) ON CONFLICT DO NOTHING""",
            (product_id, warehouse_id),
        )
        c.execute(
            """SELECT 1 FROM stock_guards WHERE product_id=%s AND warehouse_id=%s FOR UPDATE""",
            (product_id, warehouse_id),
        ).fetchone()


def stock_total(c, product_id, warehouse_id):
    return one(c, """
        SELECT
          COALESCE((SELECT sum(qty) FROM stock_movements
                    WHERE product_id=%s AND warehouse_id=%s),0)::int AS physical,
          COALESCE((SELECT sum(qty-shipped_qty) FROM reservations
                    WHERE product_id=%s AND warehouse_id=%s AND status='active'),0)::int AS reserved,
          COALESCE((SELECT sum(qty) FROM stock_blocks
                    WHERE product_id=%s AND warehouse_id=%s AND active),0)::int AS blocked
    """, (product_id, warehouse_id, product_id, warehouse_id,
          product_id, warehouse_id))


def locations(c, product_id, warehouse_id, policy="FIFO"):
    """Materialized solely from ledger entries, active reservations and blocks."""
    return all_rows(c, """
        WITH physical AS (
          SELECT bin_id,lot_id,sum(qty)::int AS physical
          FROM stock_movements WHERE product_id=%s AND warehouse_id=%s
          GROUP BY bin_id,lot_id
        ),
        holds AS (
          SELECT bin_id,lot_id,sum(qty-shipped_qty)::int AS reserved
          FROM reservations WHERE product_id=%s AND warehouse_id=%s AND status='active'
          GROUP BY bin_id,lot_id
        ),
        blocks AS (
          SELECT bin_id,lot_id,sum(qty)::int AS blocked
          FROM stock_blocks WHERE product_id=%s AND warehouse_id=%s AND active
          GROUP BY bin_id,lot_id
        )
        SELECT p.bin_id,p.lot_id,p.physical,COALESCE(h.reserved,0)::int AS reserved,
               COALESCE(b.blocked,0)::int AS blocked,
               (p.physical-COALESCE(h.reserved,0)-COALESCE(b.blocked,0))::int AS available,
               l.code AS lot_code,l.expires_at,l.created_at
        FROM physical p
        JOIN bins bn ON bn.id=p.bin_id AND bn.active
        JOIN lots l ON l.id=p.lot_id
        LEFT JOIN holds h ON h.bin_id=p.bin_id AND h.lot_id=p.lot_id
        LEFT JOIN blocks b ON b.bin_id=p.bin_id AND b.lot_id=p.lot_id
        WHERE p.physical>0
          AND (%s <> 'FEFO' OR l.expires_at IS NULL OR l.expires_at >= CURRENT_DATE)
        ORDER BY CASE WHEN %s='FEFO' THEN COALESCE(l.expires_at, 'infinity'::date) END,
                 l.created_at, p.bin_id
    """, (product_id, warehouse_id, product_id, warehouse_id,
          product_id, warehouse_id, policy, policy))


def location_available(c, product_id, warehouse_id, bin_id, lot_id):
    return one(c, """
      SELECT
        COALESCE((SELECT sum(qty) FROM stock_movements WHERE
             product_id=%s AND warehouse_id=%s AND bin_id=%s AND lot_id=%s),0)
       -COALESCE((SELECT sum(qty-shipped_qty) FROM reservations WHERE
             product_id=%s AND warehouse_id=%s AND bin_id=%s AND lot_id=%s
             AND status='active'),0)
       -COALESCE((SELECT sum(qty) FROM stock_blocks WHERE
             product_id=%s AND warehouse_id=%s AND bin_id=%s AND lot_id=%s
             AND active),0) AS available
    """, (product_id, warehouse_id, bin_id, lot_id) * 3)["available"]


def movement(c, product, warehouse, bin_id, lot, qty, doc_type, doc_id, actor):
    if qty == 0:
        return
    c.execute("""
      INSERT INTO stock_movements(product_id,warehouse_id,bin_id,lot_id,qty,doc_type,doc_id,actor_id)
      VALUES(%s,%s,%s,%s,%s,%s,%s,%s)
    """, (product, warehouse, bin_id, lot, qty, doc_type, doc_id, actor))


def replay(c, scope, key, payload):
    if not key or not 8 <= len(key) <= 128:
        error("IDEMPOTENCY_KEY", "Требуется Idempotency-Key длиной 8–128 символов")
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode()
    ).hexdigest()
    c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("idem:" + scope + ":" + key,))
    row = one(c, "SELECT * FROM idempotency_records WHERE scope=%s AND key=%s", (scope, key))
    if row:
        if row["payload_hash"] != digest:
            error("IDEMPOTENCY_CONFLICT", "Ключ использован с другим содержимым", 409)
        return digest, row["response"]
    return digest, None


def save_replay(c, scope, key, digest, response, status=200):
    c.execute("""
      INSERT INTO idempotency_records(scope,key,payload_hash,status,response)
      VALUES (%s,%s,%s,%s,%s)
    """, (scope, key, digest, status, Jsonb(response)))
