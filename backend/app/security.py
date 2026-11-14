"""PostgreSQL-backed request quotas shared by every application instance.

The ingress must be trusted explicitly before X-Real-IP is honored.
X-Forwarded-For is intentionally ignored because clients can forge its left edge.
"""
import hashlib
import ipaddress
import os
import time

from .db import one, pool

WINDOW_SECONDS = 60


def client_key(request):
    peer = request.client.host if request.client else "unknown"
    cidrs = os.getenv("TRUSTED_PROXY_CIDRS", "")
    trusted = False
    try:
        address = ipaddress.ip_address(peer)
        for text in cidrs.split(","):
            text = text.strip()
            if text and address in ipaddress.ip_network(text, strict=False):
                trusted = True
                break
    except ValueError:
        return peer
    if trusted:
        claimed = request.headers.get("x-real-ip", "")
        try:
            return str(ipaddress.ip_address(claimed))
        except ValueError:
            pass
    return str(address)


def rate_exceeded(key, limit, window_seconds=WINDOW_SECONDS):
    """Use one atomic UPSERT per request; a DB outage fails closed."""
    if limit <= 0 or window_seconds <= 0:
        raise ValueError("Rate limit configuration must be positive")
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    bucket = int(time.time()) // window_seconds
    with pool.connection() as c, c.transaction():
        count = one(c, """
          INSERT INTO rate_limit_counters(key_hash,window_id,hits)
          VALUES(%s,%s,1)
          ON CONFLICT (key_hash,window_id)
          DO UPDATE SET hits = rate_limit_counters.hits + 1
          RETURNING hits
        """, (digest, bucket))
        return count["hits"] > limit


def cleanup_rate_limits(limit=2000):
    """A bounded batched cleanup prevents the table growing indefinitely."""
    with pool.connection() as c, c.transaction():
        rows = c.execute("""
          DELETE FROM rate_limit_counters
          WHERE (key_hash,window_id) IN (
            SELECT key_hash,window_id FROM rate_limit_counters
            WHERE window_id < (extract(epoch from now())::bigint/60) - 3
            LIMIT %s FOR UPDATE SKIP LOCKED
          )
        """, (limit,))
        return rows.rowcount
