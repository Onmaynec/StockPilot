"""PostgreSQL integration checks for StockPilot acceptance invariants."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.auth import hash_password
from app.db import one, pool
from app.jobs import expire_once
from app.main import app


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as app_client:
        yield app_client


@pytest.fixture
def env(client):
    with pool.connection() as c:
        c.execute("""
          TRUNCATE TABLE audit_events, outbox_events, idempotency_records,
            return_lines, shipments, pick_tasks, reservations, order_lines, orders,
            inventory_lines, inventory_counts, transfers, receipt_lines, receipts,
            stock_blocks, stock_movements, stock_guards, lots, products, bins,
            zones, user_warehouses, warehouses, suppliers, customers, users,
            rate_limit_counters
          RESTART IDENTITY CASCADE
        """)
        wh = str(uuid4())
        zone = str(uuid4())
        bin_id = str(uuid4())
        product = str(uuid4())
        lot = str(uuid4())
        c.execute("INSERT INTO warehouses(id,code,name) VALUES(%s,'W1','Тестовый склад')", (wh,))
        c.execute("INSERT INTO zones(id,warehouse_id,code,name) VALUES(%s,%s,'A','Зона')", (zone, wh))
        c.execute("INSERT INTO bins(id,warehouse_id,zone_id,code) VALUES(%s,%s,%s,'A-01')",
                  (bin_id, wh, zone))
        c.execute("""
          INSERT INTO products(id,sku,name,unit,barcode,pick_policy)
          VALUES(%s,'SKU-001','Тестовый товар','шт','TEST-BARCODE','FEFO')
        """, (product,))
        c.execute("INSERT INTO lots(id,product_id,code,expires_at) VALUES(%s,%s,'L1',CURRENT_DATE+60)",
                  (lot, product))
        for role in ("admin", "manager", "picker", "supervisor", "director"):
            user_id = str(uuid4())
            c.execute("""
              INSERT INTO users(id,email,password_hash,role,full_name)
              VALUES(%s,%s,%s,%s,%s)
            """, (user_id, role + "@test.local", hash_password("test-password!"),
                  role, role))
            c.execute("INSERT INTO user_warehouses(user_id,warehouse_id) VALUES(%s,%s)",
                      (user_id, wh))
    tokens = {}
    for role in ("admin", "manager", "picker", "supervisor", "director"):
        r = client.post("/api/v1/auth/login", json={
            "email": role + "@test.local", "password": "test-password!"
        })
        assert r.status_code == 200, r.text
        tokens[role] = {"Authorization": "Bearer " + r.json()["accessToken"]}
    yield client, tokens, {
        "warehouse": wh, "zone": zone, "bin": bin_id, "product": product, "lot": lot,
    }


def receipt(env, qty, actual=None):
    client, users, ids = env
    r = client.post("/api/v1/receipts", headers=users["manager"], json={
        "items": [{"productId": ids["product"], "binId": ids["bin"],
                   "lotId": ids["lot"], "plannedQty": qty}]
    })
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    row = client.get("/api/v1/receipts/" + rid, headers=users["picker"]).json()["items"][0]
    fact = qty if actual is None else actual
    r = client.post("/api/v1/receipts/" + rid + "/confirm",
                    headers={**users["picker"], "Idempotency-Key": str(uuid4())},
                    json={"items": [{"lineId": row["id"], "actualQty": fact,
                                     "reason": "Недопоставка" if fact != qty else ""}]})
    assert r.status_code == 200, r.text
    return r.json()


def create_order(env, qty=1):
    client, users, ids = env
    result = client.post("/api/v1/orders", headers=users["manager"], json={
        "customer": "Тестовый клиент", "items": [{
            "productId": ids["product"], "warehouseId": ids["warehouse"], "qty": qty
        }]
    })
    assert result.status_code == 201, result.text
    return result.json()["id"]


def reserve(env, oid, key=None):
    client, users, ids = env
    return client.post("/api/v1/orders/" + oid + "/reserve",
                       headers={**users["manager"], "Idempotency-Key": key or str(uuid4())},
                       json={"ttlMinutes": 60})


def stock(env):
    client, users, ids = env
    result = client.get("/api/v1/stocks?sku=SKU-001", headers=users["manager"])
    assert result.status_code == 200, result.text
    return result.json()["items"][0]


def shipped_order(env, qty=2):
    client, users, ids = env
    receipt(env, 10)
    oid = create_order(env, qty)
    assert reserve(env, oid).status_code == 200
    order = client.get("/api/v1/orders/" + oid, headers=users["manager"]).json()
    for task in order["picks"]:
        result = client.post("/api/v1/picks/" + task["id"] + "/confirm",
                             headers=users["picker"], json={
                                 "barcode": "TEST-BARCODE", "actualQty": task["expected_qty"]
                             })
        assert result.status_code == 200, result.text
    shipment_id = order["shipments"][0]["id"]
    return oid, shipment_id, order["picks"][0]["reservation_id"]


def test_at01_at06_partial_receipt(env):
    result = receipt(env, 10, 7)
    assert result["discrepancies"][0]["difference"] == 3
    assert stock(env)["physical"] == 7
    assert stock(env)["available"] == 7


def test_at02_twenty_simultaneous_reservations(env):
    receipt(env, 8)
    orders = [create_order(env) for _ in range(20)]
    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(executor.map(lambda order_id: reserve(env, order_id), orders))
    assert sum(r.status_code == 200 for r in results) == 8
    assert sum(r.status_code == 409 for r in results) == 12, [
        (r.status_code, r.text[:150]) for r in results
    ]
    result = stock(env)
    assert (result["physical"], result["reserved"], result["available"]) == (8, 8, 0)


def test_at03_shipment_is_idempotent(env):
    client, users, ids = env
    oid, shipment_id, res_id = shipped_order(env)
    headers = {**users["picker"], "Idempotency-Key": "one-shipment-key"}
    results = [client.post("/api/v1/shipments/" + shipment_id + "/confirm",
                           headers=headers, json={}) for _ in range(5)]
    assert all(r.status_code == 200 for r in results), [r.text for r in results]
    assert all(r.json() == results[0].json() for r in results)
    assert stock(env)["physical"] == 8
    assert stock(env)["reserved"] == 0
    with pool.connection() as c:
        row = one(c, """
          SELECT count(*) AS qty FROM stock_movements
          WHERE doc_type='shipment' AND doc_id=%s
        """, (shipment_id,))
        assert row["qty"] == 1


def test_at05_inventory_separation(env):
    client, users, ids = env
    receipt(env, 10)
    r = client.post("/api/v1/inventory", headers=users["supervisor"],
                    json={"zoneId": ids["zone"]})
    assert r.status_code == 201, r.text
    count_id = r.json()["id"]
    r = client.post("/api/v1/inventory/" + count_id + "/count",
                    headers=users["supervisor"], json={"items": [{
                        "productId": ids["product"], "binId": ids["bin"],
                        "lotId": ids["lot"], "actualQty": 9
                    }]})
    assert r.status_code == 200, r.text
    assert client.post("/api/v1/inventory/" + count_id + "/submit",
                       headers=users["supervisor"]).status_code == 200
    forbidden = client.post("/api/v1/inventory/" + count_id + "/approve",
                            headers=users["supervisor"])
    assert forbidden.status_code == 403, forbidden.text
    assert stock(env)["physical"] == 10


def test_at07_return_exceeds_shipped(env):
    client, users, ids = env
    oid, shipment_id, reservation_id = shipped_order(env, 2)
    result = client.post("/api/v1/shipments/" + shipment_id + "/confirm",
                         headers={**users["picker"], "Idempotency-Key": "return-case-ship"}, json={})
    assert result.status_code == 200, result.text
    invalid = client.post("/api/v1/shipments/" + shipment_id + "/returns",
                          headers={**users["picker"], "Idempotency-Key": "return-too-large"},
                          json={"reservationId": reservation_id, "binId": ids["bin"], "qty": 3})
    assert invalid.status_code == 409, invalid.text
    assert stock(env)["physical"] == 8


def test_at08_expiration_exactly_once(env):
    client, users, ids = env
    receipt(env, 1)
    order_id = create_order(env)
    assert reserve(env, order_id).status_code == 200
    with pool.connection() as c:
        c.execute("UPDATE reservations SET expires_at=now()-interval '1 minute'")
    assert expire_once() == 1
    assert expire_once() == 0
    result = stock(env)
    assert result["reserved"] == 0
    assert result["available"] == 1
    with pool.connection() as c:
        assert one(c, """
          SELECT count(*) AS cnt FROM audit_events WHERE action='reservation.expire'
        """)["cnt"] == 1


def test_at09_csv_duplicate_has_no_side_effect(env):
    client, users, ids = env
    text = "sku,name\nA-1,Part\nA-1,Duplicate\n"
    preview = client.post("/api/v1/import/products/preview",
                          headers=users["admin"], json={"content": text})
    assert preview.status_code == 200
    assert preview.json()["valid"] is False
    commit = client.post("/api/v1/import/products/commit",
                         headers=users["admin"], json={"content": text})
    assert commit.status_code == 409
    with pool.connection() as c:
        assert one(c, "SELECT count(*) AS cnt FROM products WHERE sku='A-1'")["cnt"] == 0


def test_auth_restricts_other_roles(env):
    client, users, ids = env
    assert client.post("/api/v1/products", headers=users["picker"], json={
        "sku": "NOPE", "name": "Forbidden"
    }).status_code == 403
    assert client.get("/api/v1/audit", headers=users["manager"]).status_code == 403
    assert client.get("/api/v1/products").status_code == 401


def test_at04_transfer_rollback_on_database_failure(env):
    client, users, ids = env
    receipt(env, 5)
    second_bin = client.post("/api/v1/bins", headers=users["admin"], json={
        "zoneId": ids["zone"], "code": "A-02"
    })
    assert second_bin.status_code == 201, second_bin.text
    bid = second_bin.json()["id"]
    transfer = client.post("/api/v1/transfers", headers=users["picker"], json={
        "productId": ids["product"], "lotId": ids["lot"],
        "fromBinId": ids["bin"], "toBinId": bid, "qty": 3
    })
    assert transfer.status_code == 201, transfer.text
    tid = transfer.json()["id"]
    with pool.connection() as c:
        c.execute("""
          CREATE FUNCTION test_fail_transfer() RETURNS trigger LANGUAGE plpgsql AS $$
          BEGIN
            IF NEW.doc_type='transfer' AND NEW.qty>0 THEN
              RAISE EXCEPTION 'Synthetic failure after outbound movement';
            END IF;
            RETURN NEW;
          END; $$
        """)
        c.execute("""
          CREATE TRIGGER test_fail_transfer_trigger
          BEFORE INSERT ON stock_movements
          FOR EACH ROW EXECUTE FUNCTION test_fail_transfer()
        """)
    try:
        response = client.post("/api/v1/transfers/" + tid + "/confirm",
                               headers={**users["picker"], "Idempotency-Key": "transfer-failure-key"})
        assert response.status_code == 500, response.text
        with pool.connection() as c:
            assert one(c, """
              SELECT count(*) AS cnt FROM stock_movements
              WHERE doc_type='transfer' AND doc_id=%s
            """, (tid,))["cnt"] == 0
            assert one(c, "SELECT status FROM transfers WHERE id=%s", (tid,))["status"] == "draft"
    finally:
        with pool.connection() as c:
            c.execute("DROP TRIGGER test_fail_transfer_trigger ON stock_movements")
            c.execute("DROP FUNCTION test_fail_transfer()")


def test_reservation_replay_does_not_bypass_warehouse_rights(env):
    client, users, ids = env
    receipt(env, 1)
    oid = create_order(env)
    key = "shared-original-request"
    assert reserve(env, oid, key).status_code == 200
    with pool.connection() as c:
        outsider = str(uuid4())
        c.execute("""
          INSERT INTO users(id,email,password_hash,role,full_name)
          VALUES(%s,'outsider@test.local',%s,'manager','Outsider')
        """, (outsider, hash_password("test-password!")))
    logged_in = client.post("/api/v1/auth/login", json={
        "email": "outsider@test.local", "password": "test-password!"
    })
    token = logged_in.json()["accessToken"]
    response = client.post("/api/v1/orders/" + oid + "/reserve",
                           headers={"Authorization": "Bearer " + token,
                                    "Idempotency-Key": key},
                           json={"ttlMinutes": 60})
    assert response.status_code == 403, response.text


def test_fefo_avoids_expired_batches(env):
    client, users, ids = env
    receipt(env, 3)
    with pool.connection() as c:
        c.execute("UPDATE lots SET expires_at=CURRENT_DATE-1 WHERE id=%s", (ids["lot"],))
    replacement = client.post("/api/v1/lots", headers=users["admin"], json={
        "productId": ids["product"], "code": "L2", "expiresAt": "2099-01-01"
    })
    assert replacement.status_code == 201, replacement.text
    newlot = replacement.json()["id"]
    r = client.post("/api/v1/receipts", headers=users["manager"], json={
        "items": [{"productId": ids["product"], "binId": ids["bin"],
                   "lotId": newlot, "plannedQty": 2}]
    })
    rid = r.json()["id"]
    row = client.get("/api/v1/receipts/" + rid, headers=users["picker"]).json()["items"][0]
    confirm = client.post("/api/v1/receipts/" + rid + "/confirm",
                          headers={**users["picker"], "Idempotency-Key": str(uuid4())},
                          json={"items": [{"lineId": row["id"], "actualQty": 2}]})
    assert confirm.status_code == 200, confirm.text
    oid = create_order(env, 2)
    result = reserve(env, oid)
    assert result.status_code == 200, result.text
    with pool.connection() as c:
        allocations = c.execute("""
          SELECT lot_id FROM reservations r JOIN order_lines l ON l.id=r.order_line_id
          WHERE l.order_id=%s
        """, (oid,)).fetchall()
        assert len(allocations) == 1
        assert str(allocations[0]["lot_id"]) == str(newlot)


def test_csv_export_and_movement_filters_respect_warehouse_scope(env):
    client, users, ids = env
    receipt(env, 4)
    csv_products = client.get("/api/v1/export/products.csv", headers=users["manager"])
    assert csv_products.status_code == 200, csv_products.text
    assert "SKU-001" in csv_products.text
    csv_stocks = client.get("/api/v1/export/stocks.csv", headers=users["manager"])
    assert csv_stocks.status_code == 200, csv_stocks.text
    assert "SKU-001" in csv_stocks.text
    movements = client.get("/api/v1/movements?sku=SKU-001&warehouseId=" + ids["warehouse"],
                           headers=users["manager"])
    assert movements.status_code == 200, movements.text
    assert movements.json()["total"] == 1
    unauthorized = client.get("/api/v1/export/stocks.csv?warehouseId=" + str(uuid4()),
                              headers=users["manager"])
    assert unauthorized.status_code == 403, unauthorized.text


def test_returnable_shipment_decrements_after_return(env):
    client, users, ids = env
    _, shipment_id, reservation_id = shipped_order(env, 3)
    confirm = client.post("/api/v1/shipments/" + shipment_id + "/confirm",
                          headers={**users["picker"], "Idempotency-Key": "ship-returnable-01"},
                          json={})
    assert confirm.status_code == 200, confirm.text
    route = "/api/v1/shipments/" + shipment_id + "/returnable"
    shown = client.get(route, headers=users["manager"])
    assert shown.status_code == 200, shown.text
    assert len(shown.json()["items"]) == 1
    assert shown.json()["items"][0]["returnable_qty"] == 3
    returned = client.post("/api/v1/shipments/" + shipment_id + "/returns",
                           headers={**users["picker"], "Idempotency-Key": "return-once-01"},
                           json={"reservationId": reservation_id,
                                 "binId": ids["bin"], "qty": 2})
    assert returned.status_code == 201, returned.text
    replay = client.post("/api/v1/shipments/" + shipment_id + "/returns",
                         headers={**users["picker"], "Idempotency-Key": "return-once-01"},
                         json={"reservationId": reservation_id,
                               "binId": ids["bin"], "qty": 2})
    assert replay.status_code == 201, replay.text
    assert replay.json() == returned.json()
    assert client.get(route, headers=users["manager"]).json()["items"][0]["returnable_qty"] == 1
    assert stock(env)["physical"] == 9
    last = client.post("/api/v1/shipments/" + shipment_id + "/returns",
                       headers={**users["picker"], "Idempotency-Key": "return-once-02"},
                       json={"reservationId": reservation_id, "binId": ids["bin"], "qty": 1})
    assert last.status_code == 201, last.text
    assert client.get(route, headers=users["manager"]).json()["items"] == []
    assert stock(env)["physical"] == 10


def test_multiline_shortage_requires_supervisor_before_shipping(env):
    client, users, ids = env
    receipt(env, 12)
    order = client.post("/api/v1/orders", headers=users["manager"], json={
        "customer": "Многострочный заказ",
        "items": [
            {"productId": ids["product"], "warehouseId": ids["warehouse"], "qty": 3},
            {"productId": ids["product"], "warehouseId": ids["warehouse"], "qty": 2}
        ]
    })
    assert order.status_code == 201, order.text
    oid = order.json()["id"]
    assert reserve(env, oid).status_code == 200
    details = client.get("/api/v1/orders/" + oid, headers=users["manager"]).json()
    assert len(details["picks"]) == 2
    for index, task in enumerate(details["picks"]):
        actual = task["expected_qty"] - (1 if index == 0 else 0)
        response = client.post("/api/v1/picks/" + task["id"] + "/confirm",
                               headers=users["picker"], json={
                                   "barcode": "SKU-001", "actualQty": actual
                               })
        assert response.status_code == 200, response.text
    shipment_id = details["shipments"][0]["id"]
    blocked = client.post("/api/v1/shipments/" + shipment_id + "/confirm",
                          headers={**users["picker"], "Idempotency-Key": "blocked-shortage-01"},
                          json={})
    assert blocked.status_code == 409, blocked.text
    shortage = details["picks"][0]["id"]
    wrong_role = client.post("/api/v1/picks/" + shortage + "/approve-shortage",
                             headers=users["picker"])
    assert wrong_role.status_code == 403
    approved = client.post("/api/v1/picks/" + shortage + "/approve-shortage",
                           headers=users["supervisor"])
    assert approved.status_code == 200, approved.text
    shipped = client.post("/api/v1/shipments/" + shipment_id + "/confirm",
                          headers={**users["picker"], "Idempotency-Key": "approved-shortage-01"},
                          json={})
    assert shipped.status_code == 200, shipped.text
    assert shipped.json()["shippedQty"] == 4
    assert stock(env)["physical"] == 8
    assert stock(env)["reserved"] == 0


def test_returnable_scope_not_visible_cross_warehouse(env):
    client, users, ids = env
    _, shipment_id, _ = shipped_order(env, 1)
    assert client.post("/api/v1/shipments/" + shipment_id + "/confirm",
                       headers={**users["picker"], "Idempotency-Key": "ship-scope-01"},
                       json={}).status_code == 200
    with pool.connection() as c:
        outsider = str(uuid4())
        c.execute("""
          INSERT INTO users(id,email,password_hash,role,full_name)
          VALUES(%s,'foreign@test.local',%s,'manager','Foreign')
        """, (outsider, hash_password("test-password!")))
    logged = client.post("/api/v1/auth/login", json={
        "email": "foreign@test.local", "password": "test-password!"
    })
    assert logged.status_code == 200
    forbidden = client.get("/api/v1/shipments/" + shipment_id + "/returnable",
                           headers={"Authorization": "Bearer " + logged.json()["accessToken"]})
    assert forbidden.status_code == 403, forbidden.text


def test_inventory_variance_preview_and_backdated_movement(env):
    client, users, ids = env
    receipt(env, 10)
    started = client.post("/api/v1/inventory", headers=users["supervisor"],
                          json={"zoneId": ids["zone"]})
    assert started.status_code == 201, started.text
    count_id = started.json()["id"]
    assert started.json()["movementWatermark"] >= 1
    preview = client.get("/api/v1/inventory/" + count_id + "/variance",
                         headers=users["supervisor"])
    assert preview.status_code == 200, preview.text
    assert preview.json()["unentered"] == 1
    r = client.post("/api/v1/inventory/" + count_id + "/count",
                    headers=users["supervisor"], json={"items": [{
                        "productId": ids["product"], "binId": ids["bin"],
                        "lotId": ids["lot"], "actualQty": 8
                    }]})
    assert r.status_code == 200, r.text
    result = client.get("/api/v1/inventory/" + count_id + "/variance",
                        headers=users["supervisor"]).json()
    assert (result["deltaQty"], result["discrepancyLines"],
            result["staleSnapshot"]) == (-2, 1, False)
    assert client.post("/api/v1/inventory/" + count_id + "/submit",
                       headers=users["supervisor"]).status_code == 200
    with pool.connection() as c:
        c.execute("""
          INSERT INTO stock_movements(product_id,warehouse_id,bin_id,lot_id,
                                      qty,doc_type,doc_id,created_at)
          VALUES (%s,%s,%s,%s,1,'test-backdated',%s,now()-interval '30 days')
        """, (ids["product"], ids["warehouse"], ids["bin"], ids["lot"], str(uuid4())))
    stale = client.get("/api/v1/inventory/" + count_id + "/variance",
                       headers=users["supervisor"])
    assert stale.status_code == 200
    assert stale.json()["staleSnapshot"] is True
    rejected = client.post("/api/v1/inventory/" + count_id + "/approve",
                           headers=users["admin"])
    assert rejected.status_code == 409, rejected.text
    assert rejected.json()["code"] == "STALE_SNAPSHOT"


def test_stock_reconciliation_reports_negative_and_respects_roles(env):
    client, users, ids = env
    receipt(env, 3)
    baseline = client.get("/api/v1/stocks/anomalies", headers=users["supervisor"])
    assert baseline.status_code == 200, baseline.text
    assert baseline.json()["total"] == 0
    assert client.get("/api/v1/stocks/anomalies", headers=users["picker"]).status_code == 403
    with pool.connection() as c:
        c.execute("""
          INSERT INTO stock_movements(product_id,warehouse_id,bin_id,lot_id,qty,doc_type,doc_id)
          VALUES(%s,%s,%s,%s,-5,'synthetic-anomaly',%s)
        """, (ids["product"], ids["warehouse"], ids["bin"], ids["lot"], str(uuid4())))
    report = client.get("/api/v1/stocks/anomalies", headers=users["supervisor"]).json()
    assert report["total"] == 1
    row = report["items"][0]
    assert row["issue"] == "NEGATIVE_PHYSICAL"
    assert (row["physical"],row["available"]) == (-2,-2)
    other = str(uuid4())
    assert client.get("/api/v1/stocks/anomalies?warehouseId=" + other,
                      headers=users["supervisor"]).status_code == 403



def test_shared_rate_limiter_is_atomic_across_threads(env):
    # A single database row serializes separate workers/processes.
    from app.security import rate_exceeded
    key = "test-atomic-" + str(uuid4())
    with ThreadPoolExecutor(max_workers=12) as executor:
        decisions = list(executor.map(lambda _: rate_exceeded(key, 5, 60), range(16)))
    assert decisions.count(False) == 5
    assert decisions.count(True) == 11


def test_rate_limiter_returns_429_and_no_ip_leak(env):
    from app.security import rate_exceeded
    from app.security import client_key
    from starlette.requests import Request
    client, users, ids = env
    peer = Request({"type": "http", "headers": [(b"x-real-ip", b"192.0.2.77")],
                    "client": ("198.51.100.7", 44)})
    assert client_key(peer) == "198.51.100.7"
    from unittest.mock import patch
    with patch.dict("os.environ", {"TRUSTED_PROXY_CIDRS": "198.51.100.0/24"}):
        assert client_key(peer) == "192.0.2.77"
    # Reset the five login attempts required by this test's fixture.
    with pool.connection() as c:
        c.execute("DELETE FROM rate_limit_counters")
    # The test client key is 'testclient', identical to the middleware.
    for _ in range(30):
        assert not rate_exceeded("login:testclient", 30, 60)
    response = client.post("/api/v1/auth/login", json={
        "email": "manager@test.local", "password": "incorrect"
    })
    assert response.status_code == 429, response.text
    assert response.headers.get("retry-after") == "60"
    assert "password" not in response.text.lower()
    with pool.connection() as c:
        record = one(c, "SELECT key_hash FROM rate_limit_counters WHERE hits>=31 LIMIT 1")
        assert record is not None and "testclient" not in record["key_hash"]


def test_expired_rate_limit_window_is_cleaned(env):
    from app.security import cleanup_rate_limits
    with pool.connection() as c:
        c.execute("""
          INSERT INTO rate_limit_counters(key_hash,window_id,hits)
          VALUES('historical-window',0,5)
        """)
    assert cleanup_rate_limits() >= 1
    with pool.connection() as c:
        assert one(c, "SELECT 1 FROM rate_limit_counters WHERE key_hash='historical-window'") is None



def test_mixed_warehouse_order_is_hidden_from_partial_scope(env):
    client, users, ids = env
    other_wh = str(uuid4())
    with pool.connection() as c:
        c.execute("""
          INSERT INTO warehouses(id,code,name) VALUES(%s,'SECOND','Независимый склад')
        """, (other_wh,))
    created = client.post("/api/v1/orders", headers=users["admin"], json={
        "customer": "Чужой склад / закрытая часть",
        "items": [
            {"productId": ids["product"], "warehouseId": ids["warehouse"], "qty": 1},
            {"productId": ids["product"], "warehouseId": other_wh, "qty": 1}
        ]
    })
    assert created.status_code == 201, created.text
    oid = created.json()["id"]
    manager = client.get("/api/v1/orders", headers=users["manager"])
    assert manager.status_code == 200, manager.text
    assert oid not in {row["id"] for row in manager.json()}
    assert client.get("/api/v1/orders/" + oid, headers=users["manager"]).status_code == 403
    admin = client.get("/api/v1/orders", headers=users["admin"])
    assert oid in {row["id"] for row in admin.json()}



def test_admin_access_updates_are_atomic_and_revoke_tokens(env):
    client, users, ids = env
    with pool.connection() as c:
        employee = one(c, "SELECT id FROM users WHERE email='manager@test.local'")["id"]
    foreign = str(uuid4())
    with pool.connection() as c:
        c.execute("INSERT INTO warehouses(id,code,name) VALUES(%s,'W-NEW','Соседний склад')", (foreign,))
    wrong = client.post(f"/api/v1/users/{employee}/access",
                        headers=users["picker"], json={"warehouseIds":[foreign]})
    assert wrong.status_code == 403
    invalid = client.post(f"/api/v1/users/{employee}/access",
                          headers=users["admin"],
                          json={"warehouseIds":[foreign,str(uuid4())]})
    assert invalid.status_code == 409
    original = client.get("/api/v1/warehouses", headers=users["manager"])
    assert original.status_code == 200
    assert {row["id"] for row in original.json()} == {ids["warehouse"]}
    applied = client.post(f"/api/v1/users/{employee}/access",
                          headers=users["admin"], json={"warehouseIds":[foreign,foreign]})
    assert applied.status_code == 200, applied.text
    assert applied.json()["warehouseIds"] == [foreign]
    assert applied.json()["sessionsRevoked"] is True
    old = client.get("/api/v1/auth/me", headers=users["manager"])
    assert old.status_code == 401
    new_login = client.post("/api/v1/auth/login",
                            json={"email":"manager@test.local","password":"test-password!"})
    assert new_login.status_code == 200
    headers = {"Authorization":"Bearer " + new_login.json()["accessToken"]}
    listed = client.get("/api/v1/warehouses", headers=headers)
    assert listed.status_code == 200, listed.text
    assert {row["id"] for row in listed.json()} == {foreign}
    forbidden = client.get("/api/v1/bins?warehouseId=" + ids["warehouse"], headers=headers)
    assert forbidden.status_code == 403
    noop = client.post(f"/api/v1/users/{employee}/access", headers=users["admin"],
                       json={"warehouseIds":[foreign]})
    assert noop.status_code == 200 and noop.json()["changed"] is False
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200


def test_admin_status_revocation_and_self_disable_guard(env):
    client, users, ids = env
    with pool.connection() as c:
        admin_id = one(c, "SELECT id FROM users WHERE email='admin@test.local'")["id"]
        picker_id = one(c, "SELECT id FROM users WHERE email='picker@test.local'")["id"]
    self_block = client.post(f"/api/v1/users/{admin_id}/status",
                             headers=users["admin"], json={"active": False})
    assert self_block.status_code == 409
    assert client.get("/api/v1/auth/me", headers=users["admin"]).status_code == 200
    blocked = client.post(f"/api/v1/users/{picker_id}/status",
                          headers=users["admin"], json={"active": False})
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["sessionsRevoked"] is True
    assert client.get("/api/v1/auth/me", headers=users["picker"]).status_code == 401
    login = client.post("/api/v1/auth/login",
                        json={"email":"picker@test.local","password":"test-password!"})
    assert login.status_code == 401
    rest = client.post(f"/api/v1/users/{picker_id}/status",
                       headers=users["admin"], json={"active": True})
    assert rest.status_code == 200
    assert client.post("/api/v1/auth/login",
                       json={"email":"picker@test.local","password":"test-password!"}).status_code == 200
    audit_rows = client.get("/api/v1/audit",headers=users["admin"])
    assert audit_rows.status_code == 200
    with pool.connection() as c:
        events = c.execute("""
          SELECT action FROM audit_events
          WHERE entity_type='user' AND entity_id=%s
          ORDER BY id
        """, (str(picker_id),)).fetchall()
    assert [x["action"] for x in events] == ["user.status_change","user.status_change"]


def test_admin_users_list_scoped_and_user_creation_validates_warehouse(env):
    client, users, ids = env
    assert client.get("/api/v1/users",headers=users["manager"]).status_code == 403
    response = client.get("/api/v1/users",headers=users["admin"])
    assert response.status_code == 200
    manager = next(x for x in response.json() if x["email"]=="manager@test.local")
    assert manager["warehouse_ids"] == [ids["warehouse"]]
    bad = client.post("/api/v1/users",headers=users["admin"],json={
        "email":"provision-invalid@example.org","fullName":"Wrong warehouse",
        "role":"picker","password":"password-long-enough",
        "warehouseIds":["not-a-uuid"]
    })
    assert bad.status_code == 400
    with pool.connection() as c:
        assert one(c,"SELECT 1 FROM users WHERE email='provision-invalid@example.org'") is None
    created = client.post("/api/v1/users",headers=users["admin"],json={
        "email":"newstaff@example.org","fullName":"New employee","role":"picker",
        "password":"password-long-enough","warehouseIds":[ids["warehouse"]]
    })
    assert created.status_code == 201,created.text



def test_catalog_substring_search_and_pg_trgm_indexes(env):
    client, users, ids = env
    by_sku = client.get("/api/v1/products?q=KU-00", headers=users["manager"])
    assert by_sku.status_code == 200
    assert by_sku.json()["total"] >= 1
    by_name = client.get("/api/v1/products?q=товар", headers=users["picker"])
    assert by_name.status_code == 200
    assert by_name.json()["total"] >= 1
    by_barcode = client.get("/api/v1/products?q=BARCO", headers=users["manager"])
    assert by_barcode.status_code == 200
    assert by_barcode.json()["total"] >= 1
    with pool.connection() as c:
        indexed = {row["indexname"] for row in c.execute("""
          SELECT indexname FROM pg_indexes WHERE schemaname='public' AND tablename='products'
        """).fetchall()}
    assert {"idx_products_sku_search","idx_products_name_search",
            "idx_products_barcode_search"}.issubset(indexed)



def test_stock_summary_counts_active_reservations_and_blocks(env):
    client, users, ids = env
    receipt(env, 12)
    oid = create_order(env, 3)
    assert reserve(env, oid).status_code == 200
    blocked = client.post("/api/v1/blocks", headers=users["supervisor"], json={
        "productId": ids["product"], "binId": ids["bin"], "lotId": ids["lot"],
        "qty": 2, "reason": "Проверка качества"
    })
    assert blocked.status_code == 201, blocked.text
    detail = client.get("/api/v1/stocks?sku=SKU-001", headers=users["picker"])
    assert detail.status_code == 200, detail.text
    row = detail.json()["items"][0]
    assert (row["physical"], row["reserved"], row["blocked"], row["available"]) == (12,3,2,7)
    assert detail.json()["total"] == 1
    result = client.get("/api/v1/stocks?sku=DOES-NOT-EXIST", headers=users["picker"])
    assert result.status_code == 200 and result.json() == {
        "items": [], "total": 0, "page": 1, "limit": 50
    }
    released = client.post("/api/v1/blocks/" + blocked.json()["id"] + "/release",
                           headers=users["supervisor"])
    assert released.status_code == 200, released.text
    assert stock(env)["available"] == 9


def test_stock_page_respects_warehouse_access_and_pagination(env):
    client, users, ids = env
    receipt(env, 5)
    with pool.connection() as c, c.transaction():
        other_warehouse = str(uuid4())
        other_zone = str(uuid4())
        other_bin = str(uuid4())
        c.execute("INSERT INTO warehouses(id,code,name) VALUES (%s,'W2','Второй склад')",
                  (other_warehouse,))
        c.execute("INSERT INTO zones(id,warehouse_id,code,name) VALUES (%s,%s,'A','Зона 2')",
                  (other_zone,other_warehouse))
        c.execute("""
          INSERT INTO bins(id,warehouse_id,zone_id,code) VALUES (%s,%s,%s,'A-01')
        """,(other_bin,other_warehouse,other_zone))
        c.execute("""
          INSERT INTO stock_guards(product_id,warehouse_id) VALUES (%s,%s)
        """,(ids["product"],other_warehouse))
        c.execute("""
          INSERT INTO stock_movements(product_id,warehouse_id,bin_id,lot_id,
                                      qty,doc_type,doc_id)
          VALUES(%s,%s,%s,%s,4,'test',%s)
        """,(ids["product"],other_warehouse,other_bin,ids["lot"],str(uuid4())))
    restricted = client.get("/api/v1/stocks?sku=SKU-001",headers=users["manager"])
    assert restricted.status_code == 200
    assert restricted.json()["total"] == 1
    assert restricted.json()["items"][0]["warehouse_id"] == ids["warehouse"]
    forbidden = client.get("/api/v1/stocks?warehouseId=" + other_warehouse,
                           headers=users["manager"])
    assert forbidden.status_code == 403
    admin_page1 = client.get("/api/v1/stocks?sku=SKU-001&page=1&limit=1",
                             headers=users["admin"])
    admin_page2 = client.get("/api/v1/stocks?sku=SKU-001&page=2&limit=1",
                             headers=users["admin"])
    assert admin_page1.status_code == 200 and admin_page2.status_code == 200
    assert admin_page1.json()["total"] == 2 == admin_page2.json()["total"]
    assert {x["warehouse_id"] for x in
            [admin_page1.json()["items"][0],admin_page2.json()["items"][0]]} == {
                ids["warehouse"], other_warehouse
            }



def test_mixed_warehouse_documents_hidden_from_partial_scope(env):
    client, users, ids = env
    with pool.connection() as c, c.transaction():
        other_warehouse, other_zone, other_bin = (
            str(uuid4()),str(uuid4()),str(uuid4()))
        c.execute("INSERT INTO warehouses(id,code,name) VALUES(%s,'W2','Ограниченный склад')",
                  (other_warehouse,))
        c.execute("INSERT INTO zones(id,warehouse_id,code,name) VALUES(%s,%s,'A','Зона 2')",
                  (other_zone,other_warehouse))
        c.execute("INSERT INTO bins(id,warehouse_id,zone_id,code) VALUES(%s,%s,%s,'A-01')",
                  (other_bin,other_warehouse,other_zone))
    receipt_response = client.post("/api/v1/receipts",headers=users["admin"],
                                   json={"items":[
        {"productId":ids["product"],"binId":ids["bin"],
         "lotId":ids["lot"],"plannedQty":1},
        {"productId":ids["product"],"binId":other_bin,
         "lotId":ids["lot"],"plannedQty":1}
    ]})
    assert receipt_response.status_code == 201,receipt_response.text
    receipt_id = receipt_response.json()["id"]
    manager_receipts = client.get("/api/v1/receipts",headers=users["manager"])
    assert manager_receipts.status_code == 200
    assert receipt_id not in {r["id"] for r in manager_receipts.json()}
    assert client.get("/api/v1/receipts/" + receipt_id,
                      headers=users["manager"]).status_code == 403
    admin_receipts = client.get("/api/v1/receipts",headers=users["admin"])
    assert receipt_id in {r["id"] for r in admin_receipts.json()}

    transfer_response = client.post("/api/v1/transfers",headers=users["admin"],
                                    json={"productId":ids["product"],"lotId":ids["lot"],
        "fromBinId":ids["bin"],"toBinId":other_bin,"qty":1})
    assert transfer_response.status_code == 201,transfer_response.text
    transfer_id = transfer_response.json()["id"]
    picker_transfers = client.get("/api/v1/transfers",headers=users["picker"])
    assert picker_transfers.status_code == 200
    assert transfer_id not in {r["id"] for r in picker_transfers.json()}
    admin_transfers = client.get("/api/v1/transfers",headers=users["admin"])
    assert transfer_id in {r["id"] for r in admin_transfers.json()}



def test_pick_queue_my_assignments_and_supervisor_assignees_scope(env):
    client, users, ids = env
    receipt(env, 6)
    first = create_order(env, 2)
    second = create_order(env, 1)
    assert reserve(env, first).status_code == 200
    assert reserve(env, second).status_code == 200
    first_task = client.get("/api/v1/orders/" + first, headers=users["manager"]).json()["picks"][0]["id"]
    second_task = client.get("/api/v1/orders/" + second, headers=users["manager"]).json()["picks"][0]["id"]
    assert client.get("/api/v1/picks/assignees", headers=users["picker"]).status_code == 403
    available = client.get("/api/v1/picks/assignees?warehouseId=" + ids["warehouse"],
                           headers=users["supervisor"])
    assert available.status_code == 200, available.text
    with pool.connection() as c:
        picker_id = one(c, "SELECT id FROM users WHERE email='picker@test.local'")["id"]
    assert any(str(x["id"]) == str(picker_id) for x in available.json())
    assert all(set(x["warehouse_ids"]) <= {ids["warehouse"]} for x in available.json())
    assigned = client.post(f"/api/v1/picks/{first_task}/assign",
                           headers=users["supervisor"], json={"userId": str(picker_id)})
    assert assigned.status_code == 200, assigned.text
    mine = client.get("/api/v1/picks?assignedToMe=true", headers=users["picker"])
    assert mine.status_code == 200, mine.text
    assert {x["id"] for x in mine.json()} == {first_task}
    all_tasks = client.get("/api/v1/picks", headers=users["picker"])
    assert {first_task, second_task} <= {x["id"] for x in all_tasks.json()}
    denied = client.post(f"/api/v1/picks/{first_task}/assign",
                         headers=users["picker"], json={"userId": str(picker_id)})
    assert denied.status_code == 403
    confirmed = client.post(f"/api/v1/picks/{first_task}/confirm",
                            headers=users["picker"],
                            json={"barcode": "TEST-BARCODE", "actualQty": 2})
    assert confirmed.status_code == 200, confirmed.text


def test_picker_assignment_rejects_other_employee_and_foreign_warehouse(env):
    client, users, ids = env
    receipt(env, 4)
    order = create_order(env, 2)
    assert reserve(env, order).status_code == 200
    task_id = client.get("/api/v1/orders/" + order, headers=users["manager"]).json()["picks"][0]["id"]
    with pool.connection() as c:
        another_id = str(uuid4())
        c.execute("""INSERT INTO users(id,email,password_hash,role,full_name)
                     VALUES(%s,'picker2@test.local',%s,'picker','Second picker')""",
                  (another_id, hash_password("test-password!")))
        c.execute("INSERT INTO user_warehouses(user_id,warehouse_id) VALUES (%s,%s)",
                  (another_id, ids["warehouse"]))
        other_warehouse = str(uuid4())
        c.execute("INSERT INTO warehouses(id,code,name) VALUES(%s,'OUTSIDE','Чужой склад')",
                  (other_warehouse,))
    other_login = client.post("/api/v1/auth/login",
                              json={"email":"picker2@test.local","password":"test-password!"})
    assert other_login.status_code == 200
    other_token = {"Authorization":"Bearer " + other_login.json()["accessToken"]}
    assert client.get("/api/v1/picks/assignees?warehouseId=" + other_warehouse,
                      headers=users["supervisor"]).status_code == 403
    with pool.connection() as c:
        first_id = one(c,"SELECT id FROM users WHERE email='picker@test.local'")["id"]
    assert client.post(f"/api/v1/picks/{task_id}/assign",
                       headers=users["supervisor"],
                       json={"userId":str(first_id)}).status_code == 200
    wrong = client.post(f"/api/v1/picks/{task_id}/confirm",
                        headers=other_token,
                        json={"barcode":"TEST-BARCODE","actualQty":2})
    assert wrong.status_code == 403, wrong.text
    right = client.post(f"/api/v1/picks/{task_id}/confirm",
                        headers=users["picker"],
                        json={"barcode":"TEST-BARCODE","actualQty":1})
    assert right.status_code == 200, right.text
    assert right.json()["status"] == "shortage_pending"
    assert client.post(f"/api/v1/picks/{task_id}/approve-shortage",
                       headers=users["picker"]).status_code == 403
    approved = client.post(f"/api/v1/picks/{task_id}/approve-shortage",
                           headers=users["supervisor"])
    assert approved.status_code == 200
    with pool.connection() as c:
        row = one(c,"SELECT status,actual_qty,approved_by FROM pick_tasks WHERE id=%s",(task_id,))
    assert row["status"] == "accepted" and row["actual_qty"] == 1
