"""Opt-in mixed API write rehearsal on an isolated seeded demo environment.

Creates orders, contends for eight reserved units, and repeatedly transfers one
unit between two bins. Intentionally leaves audit/ledger entries in the demo DB.
Does NOT represent a production AT-10 signoff.
"""
import json
import math
import os
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

from .auth import issue_token
from .db import one, pool

URL = os.getenv("STOCKPILOT_WRITE_URL", "http://web/api/v1")
TRANSFERS = int(os.getenv("STOCKPILOT_WRITE_TRANSFERS", "20"))
CONTENDERS = 20


def percentile(samples, fraction):
    if not samples:
        return 0.0
    values = sorted(samples)
    return round(values[max(0, math.ceil(fraction * len(values)) - 1)], 2)


def request(token, verb, path, data=None, idem=False):
    headers = {"Authorization": "Bearer " + token,
               "Content-Type": "application/json"}
    if idem:
        headers["Idempotency-Key"] = str(uuid4())
    payload = json.dumps(data or {}).encode() if verb != "GET" else None
    req = urllib.request.Request(URL + path, data=payload, method=verb, headers=headers)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Transport error on {path}: {exc}") from exc
    elapsed_ms = (time.perf_counter() - start) * 1000
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        parsed = {}
    return status, parsed, round(elapsed_ms, 2)


def must(token, verb, path, data=None, idem=False, expected=200):
    status, response, elapsed = request(token, verb, path, data, idem)
    if status != expected:
        raise RuntimeError(f"{verb} {path}: expected {expected}; HTTP {status}: {response}")
    return response, elapsed


def run():
    if (os.getenv("STOCKPILOT_WRITE_PROFILE_ALLOW") != "1" or
            os.getenv("STOCKPILOT_WRITE_PROFILE_ISOLATED") != "1"):
        raise SystemExit(
            "Requires STOCKPILOT_WRITE_PROFILE_ALLOW=1 and "
            "STOCKPILOT_WRITE_PROFILE_ISOLATED=1 on a disposable demo database."
        )
    if not 1 <= TRANSFERS <= 1500:
        raise SystemExit("Transfers must be 1..1500")
    pool.open(wait=True)
    try:
        with pool.connection() as c:
            admin = one(c, """
              SELECT id,token_version FROM users
              WHERE email='admin@stockpilot.local' AND active
            """)
            seeded = one(c, """
              SELECT p.id AS product_id,sg.warehouse_id,lot.id AS lot_id,
                     b.id AS bin_id,b.zone_id
              FROM products p
              JOIN stock_guards sg ON sg.product_id=p.id
              JOIN lots lot ON lot.product_id=p.id
              JOIN bins b ON b.warehouse_id=sg.warehouse_id AND b.code='A-01'
              WHERE p.sku='SKU-00001' LIMIT 1
            """)
            seed_count = one(c, """
              SELECT count(*)::int AS total FROM stock_movements
              WHERE doc_type='seed'
            """)["total"]
            if not admin or not seeded or seed_count != 12000:
                raise RuntimeError("Refusing run without exact 12k-movement demo seed")
            original = one(c, """
              SELECT COALESCE(sum(qty),0)::int AS qty FROM stock_movements
              WHERE product_id=%s AND warehouse_id=%s
            """, (seeded["product_id"], seeded["warehouse_id"]))["qty"]
            if original < 1:
                raise RuntimeError("Seed SKU-00001 must have one or more units")
        token = issue_token(admin)
        test_id = uuid4().hex[:11].upper()
        create_times, receipt_times, reserve_times, transfer_times = [], [], [], []
        started = time.perf_counter()
        warehouse = str(seeded["warehouse_id"])
        source_bin = str(seeded["bin_id"])
        target, _ = must(token, "POST", "/bins",
                         {"zoneId": str(seeded["zone_id"]), "code": "P-" + test_id},
                         expected=201)
        product, _ = must(token, "POST", "/products", {
            "sku": "PERF-" + test_id, "name": "Контрольный товар " + test_id,
            "category": "Тестовая нагрузка", "pickPolicy": "FIFO",
        }, expected=201)
        product_id = product["id"]
        lot, _ = must(token, "POST", "/lots", {
            "productId": product_id, "code": "P-" + test_id,
        }, expected=201)
        receipt, receipt_time = must(token, "POST", "/receipts", {"items": [{
            "productId": product_id, "binId": source_bin,
            "lotId": lot["id"], "plannedQty": 8,
        }]}, expected=201)
        receipt_times.append(receipt_time)
        line, _ = must(token, "GET", "/receipts/" + receipt["id"])
        posted, posted_time = must(token, "POST",
                                  "/receipts/" + receipt["id"] + "/confirm",
                                  {"items": [{"lineId": line["items"][0]["id"],
                                              "actualQty": 8, "reason": ""}]},
                                  idem=True)
        receipt_times.append(posted_time)
        if posted.get("status") != "posted":
            raise RuntimeError("Receipt failed to post")
        order_ids = []
        for i in range(CONTENDERS):
            result, elapsed = must(token, "POST", "/orders", {
                "customer": "Perf " + test_id + f"-{i}",
                "items": [{
                    "productId": product_id, "warehouseId": warehouse, "qty": 1,
                }],
            }, expected=201)
            order_ids.append(result["id"])
            create_times.append(elapsed)

        def contender(order_id):
            return request(token, "POST", f"/orders/{order_id}/reserve",
                           {"ttlMinutes": 60}, idem=True)

        race_started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=20) as executor:
            decisions = list(executor.map(contender, order_ids))
        race_seconds = time.perf_counter() - race_started
        reserve_times = [x[2] for x in decisions]
        success = sum(x[0] == 200 for x in decisions)
        shortage = sum(x[0] == 409 and x[1].get("code") == "INSUFFICIENT_STOCK"
                       for x in decisions)
        if success != 8 or shortage != 12:
            raise RuntimeError(
                f"Reservation race failed: successes={success}, shortage={shortage}; "
                f"responses={[(x[0], x[1].get('code')) for x in decisions]}"
            )
        transfers = []
        for i in range(TRANSFERS):
            from_bin, to_bin = (source_bin, target["id"]) if i % 2 == 0 \
                else (target["id"], source_bin)
            transfer, _ = must(token, "POST", "/transfers", {
                "productId": str(seeded["product_id"]),
                "lotId": str(seeded["lot_id"]), "fromBinId": from_bin,
                "toBinId": to_bin, "qty": 1,
            }, expected=201)
            confirmation, elapsed = must(
                token, "POST", "/transfers/" + transfer["id"] + "/confirm",
                {}, idem=True)
            if confirmation.get("status") != "posted":
                raise RuntimeError("Transfer not posted")
            transfer_times.append(elapsed)
            transfers.append(transfer["id"])

        with pool.connection() as c:
            new_stock = one(c, """
              SELECT COALESCE(sum(qty),0)::int AS physical
              FROM stock_movements WHERE product_id=%s
            """, (product_id,))["physical"]
            reserved = one(c, """
              SELECT COALESCE(sum(qty-shipped_qty),0)::int AS reserved
              FROM reservations WHERE product_id=%s AND status='active'
            """, (product_id,))["reserved"]
            after = one(c, """
              SELECT COALESCE(sum(qty),0)::int AS qty
              FROM stock_movements WHERE product_id=%s AND warehouse_id=%s
            """, (seeded["product_id"], seeded["warehouse_id"]))["qty"]
            moves = one(c, """
              SELECT count(*)::int AS cnt,COALESCE(sum(qty),0)::int AS net
              FROM stock_movements WHERE doc_type='transfer'
                AND doc_id=ANY(%s::uuid[])
            """, (transfers,))
        valid = (new_stock, reserved, after, moves["cnt"], moves["net"]) == (
            8,8,original,TRANSFERS*2,0)
        report = {
            "profile": "isolated-api-mixed-write-rehearsal",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ordersCreated": CONTENDERS, "reservationsWon": success,
            "expectedOutOfStock": shortage,
            "transfersPosted": TRANSFERS, "ledgerMovementRows": moves["cnt"],
            "netMovement": moves["net"],
            "invariants": {
                "testPhysical": new_stock, "testReserved": reserved,
                "originalStock": original, "stockAfterTransfers": after,
                "passed": valid,
            },
            "reservationRaceSeconds": round(race_seconds, 2),
            "elapsedSeconds": round(time.perf_counter() - started, 2),
            "p95Ms": {
                "orderCreate": percentile(create_times,.95),
                "receiptPost": percentile(receipt_times,.95),
                "reserve": percentile(reserve_times,.95),
                "transferPost": percentile(transfer_times,.95),
            },
            "limitations": (
                "Real HTTP writes on disposable seed; smoke uses 20 orders and "
                "configurable transfers. Not full AT-10: no 350-order workload "
                "or 60 writer identities, no production SLA or RTO proof."
            ),
        }
        print(json.dumps(report,ensure_ascii=False,indent=2))
        if not valid:
            raise SystemExit(1)
    finally:
        pool.close()


if __name__ == "__main__":
    run()
