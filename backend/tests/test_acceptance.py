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
            zones, user_warehouses, warehouses, suppliers, customers, users
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
