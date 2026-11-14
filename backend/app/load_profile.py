"""Concurrent, authenticated read baseline on an opted-in seeded demo database."""
import json
import math
import os
import platform
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

from .auth import hash_password, issue_token
from .db import one, pool

USERS = int(os.getenv("STOCKPILOT_LOAD_USERS", "60"))
PER_USER = int(os.getenv("STOCKPILOT_LOAD_REQUESTS", "10"))
URL = os.getenv("STOCKPILOT_LOAD_URL", "http://web/api/v1")


def percentile(values, percent):
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(len(ordered) * percent / 100) - 1)], 2)


def memory_kib():
    try:
        with open("/proc/1/status", encoding="utf-8") as proc:
            for line in proc:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (OSError, ValueError):
        return None
    return None


def request(token, path):
    before = time.perf_counter()
    req = urllib.request.Request(URL + path, headers={"Authorization": "Bearer " + token})
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (urllib.error.URLError, TimeoutError, OSError):
        status = 0
    return status, round((time.perf_counter() - before) * 1000, 2)


def task(token, index, barrier):
    barrier.wait(timeout=45)
    values = []
    for i in range(PER_USER):
        category = "catalog" if i % 2 == 0 else "stocks"
        sku = f"SKU-{(index * 19 + i) % 12000 + 1:05d}"
        path = ("/products?q=" if category == "catalog" else "/stocks?sku=") + sku
        status, duration = request(token, path)
        values.append((category, status, duration))
    return values


def main():
    if os.getenv("STOCKPILOT_PERF_ALLOW") != "1":
        raise SystemExit("Benchmark requires STOCKPILOT_PERF_ALLOW=1 on an isolated demo.")
    if not 1 <= USERS <= 100 or not 1 <= PER_USER <= 50:
        raise SystemExit("Invalid benchmark size.")
    pool.open(wait=True)
    identities = []
    uid = uuid4().hex[:10]
    try:
        with pool.connection() as c, c.transaction():
            if not one(c, "SELECT 1 FROM products WHERE sku='SKU-00001'"):
                raise RuntimeError("Demo seed required.")
            wh = one(c, "SELECT id FROM warehouses ORDER BY code LIMIT 1")
            if not wh:
                raise RuntimeError("Demo warehouse required.")
            hashed = hash_password(uuid4().hex)
            for i in range(USERS):
                row = one(c, """
                  INSERT INTO users(email,password_hash,role,full_name)
                  VALUES(%s,%s,'manager',%s) RETURNING id,token_version
                """, (f"bench-{uid}-{i}@stockpilot.invalid", hashed, f"Benchmark {i}"))
                identities.append(row)
                c.execute("INSERT INTO user_warehouses(user_id,warehouse_id) VALUES(%s,%s)",
                          (row["id"], wh["id"]))
            pg_version = one(c, "SHOW server_version")["server_version"]
        tokens = [issue_token(row) for row in identities]
        if request(tokens[0], "/products?q=SKU-00001")[0] != 200:
            raise RuntimeError("Warm-up HTTP request failed")
        barrier = Barrier(USERS)
        memory_before = memory_kib()
        begin = time.perf_counter()
        with ThreadPoolExecutor(max_workers=USERS) as executor:
            outcome = list(executor.map(lambda entry: task(*entry, barrier),
                                        ((tok, i) for i, tok in enumerate(tokens))))
        wall = time.perf_counter() - begin
        memory_after = memory_kib()
        observations = [item for worker in outcome for item in worker]
        groups = {}
        for name in ("catalog", "stocks"):
            rows = [item for item in observations if item[0] == name]
            timing = [row[2] for row in rows]
            groups[name] = {"requests": len(rows), "errors": sum(r[1] != 200 for r in rows),
                            "p50Ms": percentile(timing, 50), "p95Ms": percentile(timing, 95),
                            "p99Ms": percentile(timing, 99), "maxMs": round(max(timing), 2)}
        errors = sum(row[1] != 200 for row in observations)
        print(json.dumps({
            "scenario": "read-only authenticated baseline",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "distinctUsers": USERS, "requestsPerUser": PER_USER,
            "requests": len(observations), "errors": errors,
            "durationSeconds": round(wall, 2),
            "requestsPerSecond": round(len(observations) / wall, 2),
            "latency": groups,
            "environment": {"python": platform.python_version(), "postgres": pg_version,
                            "apiProcessMemoryKiBBefore": memory_before,
                            "apiProcessMemoryKiBAfter": memory_after},
            "limitations": "No 350 orders/3000 movements write mix. Not full AT-10 signoff.",
        }, ensure_ascii=False, indent=2))
        if errors:
            raise SystemExit(1)
    finally:
        try:
            with pool.connection() as c, c.transaction():
                if identities:
                    ids = [row["id"] for row in identities]
                    c.execute("DELETE FROM user_warehouses WHERE user_id=ANY(%s::uuid[])", (ids,))
                    c.execute("DELETE FROM users WHERE id=ANY(%s::uuid[])", (ids,))
        finally:
            pool.close()


if __name__ == "__main__":
    main()
