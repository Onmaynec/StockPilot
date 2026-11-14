"""Opt-in 60-user mixed API write profile on a disposable demo database.

Creates 350 orders, races 20 reservations against eight units, and posts 1,500
transfers (3,000 append-only ledger movements). Thirty managers and thirty
pickers issue authenticated requests from distinct test-only source addresses.
The workload deliberately leaves business documents and ledger rows in the demo DB.
It is not a production capacity certification.
"""
import ipaddress
import json
import math
import os
import platform
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier
from uuid import uuid4

from .auth import hash_password, issue_token
from .db import one, pool
from .perf_metrics import resource_delta, resource_snapshot

URL = os.getenv("STOCKPILOT_WRITE_URL", "http://127.0.0.1:8000/api/v1")
ORDERS = int(os.getenv("STOCKPILOT_WRITE_ORDERS", "350"))
TRANSFERS = int(os.getenv("STOCKPILOT_WRITE_TRANSFERS", "1500"))
CONTENDERS = 20
MANAGERS = 30
PICKERS = 30
TARGET_P95_MS = int(os.getenv("STOCKPILOT_WRITE_P95_LIMIT_MS", "1500"))


def percentile(samples, fraction):
    if not samples:
        return 0.0
    values = sorted(samples)
    return round(values[max(0, math.ceil(fraction * len(values)) - 1)], 2)


def distribution(samples):
    return {
        "samples": len(samples),
        "p50Ms": percentile(samples, .50),
        "p95Ms": percentile(samples, .95),
        "p99Ms": percentile(samples, .99),
        "maxMs": round(max(samples), 2) if samples else 0.0,
    }


def verify_isolated_loopback():
    if (os.getenv("STOCKPILOT_WRITE_PROFILE_ALLOW") != "1" or
            os.getenv("STOCKPILOT_WRITE_PROFILE_ISOLATED") != "1"):
        raise SystemExit(
            "Requires STOCKPILOT_WRITE_PROFILE_ALLOW=1 and "
            "STOCKPILOT_WRITE_PROFILE_ISOLATED=1 on a disposable demo database."
        )
    if urllib.parse.urlparse(URL).hostname != "127.0.0.1":
        raise SystemExit("Write profile must call the API through 127.0.0.1 only.")
    peer = ipaddress.ip_address("127.0.0.1")
    trusted = False
    for entry in os.getenv("TRUSTED_PROXY_CIDRS", "").split(","):
        try:
            if entry.strip() and peer in ipaddress.ip_network(entry.strip(), strict=False):
                trusted = True
                break
        except ValueError:
            continue
    if not trusted:
        raise SystemExit(
            "The isolated API must trust 127.0.0.1/32 for test-only X-Real-IP values."
        )
    if not 30 <= ORDERS <= 1000:
        raise SystemExit("STOCKPILOT_WRITE_ORDERS must be 30..1000.")
    if not 60 <= TRANSFERS <= 1500 or TRANSFERS % 60:
        raise SystemExit("STOCKPILOT_WRITE_TRANSFERS must be a multiple of 60 in 60..1500.")
    if TARGET_P95_MS <= 0:
        raise SystemExit("STOCKPILOT_WRITE_P95_LIMIT_MS must be positive.")


def request(token, verb, path, data=None, idem=False, source_ip=None):
    headers = {"Authorization": "Bearer " + token,
               "Content-Type": "application/json"}
    if idem:
        headers["Idempotency-Key"] = str(uuid4())
    if source_ip:
        headers["X-Real-IP"] = source_ip
    payload = json.dumps(data or {}).encode() if verb != "GET" else None
    req = urllib.request.Request(URL + path, data=payload, method=verb, headers=headers)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
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


def must(token, verb, path, data=None, idem=False, expected=200, source_ip=None):
    status, response, elapsed = request(token, verb, path, data, idem, source_ip)
    if status != expected:
        raise RuntimeError(f"{verb} {path}: expected {expected}; HTTP {status}: {response}")
    return response, elapsed


def create_order(manager, index, product_id, warehouse):
    result, elapsed = must(
        manager["token"], "POST", "/orders", {
            "customer": f"Perf {index}",
            "items": [{"productId": product_id, "warehouseId": warehouse, "qty": 1}],
        }, expected=201, source_ip=manager["source_ip"])
    return index, result["id"], elapsed


def create_transfer(picker, product_id, lot_id, source_bin, target_bin):
    result, elapsed = must(
        picker["token"], "POST", "/transfers", {
            "productId": product_id, "lotId": lot_id,
            "fromBinId": source_bin, "toBinId": target_bin, "qty": 1,
        }, expected=201, source_ip=picker["source_ip"])
    return result["id"], elapsed


def confirm_transfer(picker, transfer_id):
    result, elapsed = must(
        picker["token"], "POST", "/transfers/" + transfer_id + "/confirm",
        {}, idem=True, source_ip=picker["source_ip"])
    if result.get("status") != "posted":
        raise RuntimeError("Transfer was not posted: " + str(result))
    return elapsed


def move_once(index, picker, product_id, lot_id, source_bin, target_bin):
    transfer_id, created = create_transfer(
        picker, product_id, lot_id, source_bin, target_bin)
    confirmed = confirm_transfer(picker, transfer_id)
    return transfer_id, created, confirmed


def run():
    verify_isolated_loopback()
    pool.open(wait=True)
    identities = []
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
            postgres = one(c, "SHOW server_version")["server_version"]
            db_settings = one(c, """
              SELECT current_setting('shared_buffers') AS shared_buffers,
                     current_setting('work_mem') AS work_mem,
                     current_setting('effective_cache_size') AS effective_cache_size,
                     current_setting('max_connections') AS max_connections,
                     current_setting('random_page_cost') AS random_page_cost
            """)
            password_hash = hash_password(uuid4().hex)
            run_id = uuid4().hex[:10]
            for index in range(MANAGERS + PICKERS):
                role = "manager" if index < MANAGERS else "picker"
                row = one(c, """
                  INSERT INTO users(email,password_hash,role,full_name)
                  VALUES(%s,%s,%s,%s) RETURNING id,token_version
                """, (f"writebench-{run_id}-{index}@stockpilot.invalid",
                      password_hash, role, f"Write profile {index + 1}"))
                c.execute("INSERT INTO user_warehouses(user_id,warehouse_id) VALUES(%s,%s)",
                          (row["id"], seeded["warehouse_id"]))
                identities.append({
                    "id": str(row["id"]), "role": role,
                    "token": issue_token(row),
                    "source_ip": f"198.51.100.{index + 1}",
                })
        admin_token = issue_token(admin)
        managers = identities[:MANAGERS]
        pickers = identities[MANAGERS:]
        warehouse = str(seeded["warehouse_id"])
        source_bin = str(seeded["bin_id"])
        test_id = uuid4().hex[:11].upper()
        resources_before = resource_snapshot()
        started = time.perf_counter()

        # Eight physical units form an intentionally oversubscribed reservation race.
        reserve_product, _ = must(admin_token, "POST", "/products", {
            "sku": "PERF-RES-" + test_id, "name": "Контроль резервов " + test_id,
            "category": "Тестовая нагрузка", "pickPolicy": "FIFO",
        }, expected=201)
        reserve_lot, _ = must(admin_token, "POST", "/lots", {
            "productId": reserve_product["id"], "code": "PERF-RES-" + test_id,
        }, expected=201)
        reserve_receipt, _ = must(managers[0]["token"], "POST", "/receipts", {
            "items": [{"productId": reserve_product["id"], "binId": source_bin,
                       "lotId": reserve_lot["id"], "plannedQty": 8}],
        }, expected=201, source_ip=managers[0]["source_ip"])
        reserve_line, _ = must(managers[0]["token"], "GET",
                               "/receipts/" + reserve_receipt["id"],
                               source_ip=managers[0]["source_ip"])
        posted, receipt_elapsed = must(
            pickers[0]["token"], "POST", "/receipts/" + reserve_receipt["id"] + "/confirm",
            {"items": [{"lineId": reserve_line["items"][0]["id"],
                        "actualQty": 8, "reason": ""}]},
            idem=True, source_ip=pickers[0]["source_ip"])
        if posted.get("status") != "posted":
            raise RuntimeError("Reservation test receipt was not posted")
        receipt_times = [receipt_elapsed]

        # A separate 1,500-unit test SKU supports 1,500 reversible transfers.
        transfer_product, _ = must(admin_token, "POST", "/products", {
            "sku": "PERF-MOVE-" + test_id, "name": "Контроль перемещений " + test_id,
            "category": "Тестовая нагрузка", "pickPolicy": "FIFO",
        }, expected=201)
        transfer_lot, _ = must(admin_token, "POST", "/lots", {
            "productId": transfer_product["id"], "code": "PERF-MOVE-" + test_id,
        }, expected=201)
        transfer_receipt, _ = must(managers[1]["token"], "POST", "/receipts", {
            "items": [{"productId": transfer_product["id"], "binId": source_bin,
                       "lotId": transfer_lot["id"], "plannedQty": TRANSFERS}],
        }, expected=201, source_ip=managers[1]["source_ip"])
        transfer_line, _ = must(managers[1]["token"], "GET",
                                "/receipts/" + transfer_receipt["id"],
                                source_ip=managers[1]["source_ip"])
        posted, transfer_receipt_elapsed = must(
            pickers[1]["token"], "POST",
            "/receipts/" + transfer_receipt["id"] + "/confirm",
            {"items": [{"lineId": transfer_line["items"][0]["id"],
                        "actualQty": TRANSFERS, "reason": ""}]},
            idem=True, source_ip=pickers[1]["source_ip"])
        if posted.get("status") != "posted":
            raise RuntimeError("Transfer test receipt was not posted")
        receipt_times.append(transfer_receipt_elapsed)

        target, _ = must(admin_token, "POST", "/bins", {
            "zoneId": str(seeded["zone_id"]), "code": "P-" + test_id,
        }, expected=201)
        target_bin = target["id"]
        wave_size = PICKERS
        transfer_create_times = []
        transfer_confirm_times = []
        transfer_ids = []

        # Prepare 30 transfer documents before a synchronized 60-request wave.
        with ThreadPoolExecutor(max_workers=PICKERS) as executor:
            prepared = list(executor.map(
                lambda i: create_transfer(pickers[i], transfer_product["id"],
                                          transfer_lot["id"], source_bin, target_bin),
                range(wave_size)))
        wave_transfers = [item[0] for item in prepared]
        transfer_create_times.extend(item[1] for item in prepared)

        barrier = Barrier(MANAGERS + PICKERS)

        def order_wave(index):
            barrier.wait(timeout=60)
            return create_order(managers[index], index, reserve_product["id"], warehouse)

        def transfer_wave(index):
            barrier.wait(timeout=60)
            elapsed = confirm_transfer(pickers[index], wave_transfers[index])
            return index, elapsed

        # 30 order writers + 30 transfer posters start together: 60 distinct accounts.
        with ThreadPoolExecutor(max_workers=MANAGERS + PICKERS) as executor:
            order_futures = [executor.submit(order_wave, i) for i in range(MANAGERS)]
            transfer_futures = [executor.submit(transfer_wave, i) for i in range(PICKERS)]
            wave_orders = [future.result() for future in order_futures]
            wave_confirmations = [future.result() for future in transfer_futures]
        order_ids = [None] * ORDERS
        order_create_times = [0.0] * ORDERS
        for index, order_id, elapsed in wave_orders:
            order_ids[index] = order_id
            order_create_times[index] = elapsed
        transfer_confirm_times.extend(elapsed for _, elapsed in wave_confirmations)
        transfer_ids.extend(wave_transfers)

        # Complete the remaining order documents with the same 30 manager identities.
        def create_remaining(index):
            return create_order(managers[index % MANAGERS], index,
                                reserve_product["id"], warehouse)
        with ThreadPoolExecutor(max_workers=MANAGERS) as executor:
            remaining_orders = list(executor.map(create_remaining, range(wave_size, ORDERS)))
        for index, order_id, elapsed in remaining_orders:
            order_ids[index] = order_id
            order_create_times[index] = elapsed
        if any(order_id is None for order_id in order_ids):
            raise RuntimeError("Order workload did not create every requested document")

        # Exactly 8 of 20 concurrent reservations may win against eight units.
        def contender(index):
            return request(
                managers[index]["token"], "POST",
                "/orders/" + order_ids[index] + "/reserve",
                {"ttlMinutes": 60}, idem=True, source_ip=managers[index]["source_ip"])
        with ThreadPoolExecutor(max_workers=CONTENDERS) as executor:
            decisions = list(executor.map(contender, range(CONTENDERS)))
        reserve_times = [item[2] for item in decisions]
        success = sum(item[0] == 200 for item in decisions)
        shortage = sum(item[0] == 409 and item[1].get("code") == "INSUFFICIENT_STOCK"
                       for item in decisions)
        if success != 8 or shortage != 12:
            raise RuntimeError(
                f"Reservation race failed: successes={success}, shortage={shortage}; "
                f"responses={[(x[0], x[1].get('code')) for x in decisions]}"
            )

        # Return the initial wave, then process alternating batches of 30 each way.
        def batch_moves(indices, from_bin, to_bin):
            with ThreadPoolExecutor(max_workers=PICKERS) as executor:
                return list(executor.map(
                    lambda i: move_once(i, pickers[i % PICKERS],
                                        transfer_product["id"], transfer_lot["id"],
                                        from_bin, to_bin), indices))
        reversed_wave = batch_moves(range(wave_size), target_bin, source_bin)
        for transfer_id, created, confirmed in reversed_wave:
            transfer_ids.append(transfer_id)
            transfer_create_times.append(created)
            transfer_confirm_times.append(confirmed)

        remaining = TRANSFERS - 2 * wave_size
        for offset in range(0, remaining, PICKERS):
            outbound = (offset // PICKERS) % 2 == 0
            from_bin, to_bin = (source_bin, target_bin) if outbound else (target_bin, source_bin)
            end = min(offset + PICKERS, remaining)
            batch = batch_moves(range(offset, end), from_bin, to_bin)
            for transfer_id, created, confirmed in batch:
                transfer_ids.append(transfer_id)
                transfer_create_times.append(created)
                transfer_confirm_times.append(confirmed)

        with pool.connection() as c:
            reserve_stock = one(c, """
              SELECT COALESCE(sum(qty),0)::int AS physical
              FROM stock_movements WHERE product_id=%s
            """, (reserve_product["id"],))["physical"]
            reserved = one(c, """
              SELECT COALESCE(sum(qty-shipped_qty),0)::int AS reserved
              FROM reservations WHERE product_id=%s AND status='active'
            """, (reserve_product["id"],))["reserved"]
            seed_after = one(c, """
              SELECT COALESCE(sum(qty),0)::int AS qty FROM stock_movements
              WHERE product_id=%s AND warehouse_id=%s
            """, (seeded["product_id"], seeded["warehouse_id"]))["qty"]
            transfer_totals = one(c, """
              SELECT COALESCE(sum(qty),0)::int AS total,
                     COALESCE(sum(qty) FILTER (WHERE bin_id=%s),0)::int AS source,
                     COALESCE(sum(qty) FILTER (WHERE bin_id=%s),0)::int AS target
              FROM stock_movements WHERE product_id=%s AND warehouse_id=%s
            """, (source_bin, target_bin, transfer_product["id"], warehouse))
            moves = one(c, """
              SELECT count(*)::int AS cnt,COALESCE(sum(qty),0)::int AS net
              FROM stock_movements WHERE doc_type='transfer'
                AND doc_id=ANY(%s::uuid[])
            """, (transfer_ids,))
            posted_count = one(c, """
              SELECT count(*)::int AS cnt FROM transfers
              WHERE id=ANY(%s::uuid[]) AND status='posted'
            """, (transfer_ids,))["cnt"]
        valid = (
            reserve_stock == 8 and reserved == 8 and seed_after == original
            and transfer_totals["total"] == TRANSFERS
            and transfer_totals["source"] == TRANSFERS
            and transfer_totals["target"] == 0
            and moves["cnt"] == TRANSFERS * 2 and moves["net"] == 0
            and posted_count == TRANSFERS and len(transfer_ids) == TRANSFERS
        )
        latency = {
            "orderCreate": distribution(order_create_times),
            "receiptPost": distribution(receipt_times),
            "reserve": distribution(reserve_times),
            "transferCreate": distribution(transfer_create_times),
            "transferConfirm": distribution(transfer_confirm_times),
        }
        thresholds = {
            name: {"targetP95Ms": TARGET_P95_MS, "actualP95Ms": metric["p95Ms"],
                   "passed": metric["p95Ms"] <= TARGET_P95_MS}
            for name, metric in latency.items()
        }
        elapsed = time.perf_counter() - started
        resources_after = resource_snapshot()
        report = {
            "profile": "isolated-60-user-mixed-write-acceptance",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "distinctUsers": len(identities),
            "distinctTestSourceIPs": len({item["source_ip"] for item in identities}),
            "simultaneousWriteWave": {"operations": MANAGERS + PICKERS,
                                      "orderWriters": MANAGERS,
                                      "transferPosters": PICKERS},
            "ordersCreated": len(order_ids),
            "reservationsWon": success,
            "expectedOutOfStock": shortage,
            "transfersPosted": len(transfer_ids),
            "ledgerMovementRows": moves["cnt"],
            "netMovement": moves["net"],
            "httpErrors": 0,
            "invariants": {
                "reservationProductPhysical": reserve_stock,
                "reservationProductReserved": reserved,
                "originalSeedStock": original,
                "seedStockAfter": seed_after,
                "transferProductPhysical": transfer_totals["total"],
                "transferSourceBin": transfer_totals["source"],
                "transferTargetBin": transfer_totals["target"],
                "postedTransferDocuments": posted_count,
                "passed": valid,
            },
            "latency": latency,
            "thresholds": thresholds,
            "elapsedSeconds": round(elapsed, 2),
            "requestsExecuted": ORDERS + CONTENDERS + TRANSFERS * 2 + 11,
            "environment": {
                "python": platform.python_version(),
                "platform": platform.platform(),
                "postgres": postgres,
                "databaseSettings": db_settings,
                "hostCpuCount": os.cpu_count(),
            },
            "resources": resource_delta(resources_before, resources_after, elapsed),
            "limitations": (
                "Single isolated CI/demo run, not a 24-hour production soak. "
                "Hardware, ingress, external integrations, and production data shape differ."
            ),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not valid or not all(item["passed"] for item in thresholds.values()):
            raise SystemExit("Mixed-write profile failed: invariant or p95 target exceeded.")
    finally:
        try:
            if identities:
                with pool.connection() as c, c.transaction():
                    ids = [item["id"] for item in identities]
                    c.execute("""
                      UPDATE users SET active=false,token_version=token_version+1
                      WHERE id=ANY(%s::uuid[])
                    """, (ids,))
        finally:
            pool.close()


if __name__ == "__main__":
    run()
