"""Catalog, warehouses, authentication, reporting, CSV and audit APIs."""
import csv
import io
import os
from collections import Counter
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field
from psycopg.errors import UniqueViolation

from .auth import current_user, hash_password, issue_token, require, verify_password, warehouse_access
from .common import check_bin, check_lot, clean, require_row
from .db import all_rows, audit, error, locations, lock_stock, one, pool, stock_total

router = APIRouter()


class Login(BaseModel):
    email: str
    password: str


class ProductIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=250)
    unit: str = Field(default="шт", max_length=30)
    barcode: str | None = Field(default=None, max_length=100)
    category: str = Field(default="Общее", max_length=100)
    pickPolicy: str = "FIFO"
    supplierId: str | None = None


class WarehouseIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=150)


class ZoneIn(BaseModel):
    warehouseId: str
    code: str
    name: str


class BinIn(BaseModel):
    zoneId: str
    code: str


class LotIn(BaseModel):
    productId: str
    code: str
    expiresAt: str | None = None


class UserIn(BaseModel):
    email: str
    fullName: str
    role: str
    password: str = Field(min_length=12)
    warehouseIds: list[str] = []


class CsvPayload(BaseModel):
    content: str = Field(max_length=2_000_000)


@router.post("/auth/login", tags=["Auth"])
def login(data: Login):
    with pool.connection() as c:
        user = one(c, "SELECT * FROM users WHERE lower(email)=lower(%s)", (data.email,))
        if not user or not user["active"] or not verify_password(data.password, user["password_hash"]):
            error("BAD_CREDENTIALS", "Неверная почта или пароль", 401)
        return {"accessToken": issue_token(user), "tokenType": "bearer"}


@router.get("/auth/me", tags=["Auth"])
def me(user=Depends(current_user)):
    return clean(user)


@router.post("/users", tags=["Admin"], status_code=201)
def add_user(data: UserIn, user=Depends(require("admin"))):
    if data.role not in {"admin", "manager", "picker", "supervisor", "director"}:
        error("INVALID_ROLE", "Недопустимая роль")
    try:
        with pool.connection() as c, c.transaction():
            row = one(c, """
              INSERT INTO users(email,password_hash,role,full_name)
              VALUES(lower(%s),%s,%s,%s) RETURNING id,email,role,full_name
            """, (data.email, hash_password(data.password), data.role, data.fullName))
            for warehouse in set(data.warehouseIds):
                require_row(c, "warehouses", warehouse)
                c.execute("INSERT INTO user_warehouses VALUES(%s,%s)", (row["id"], warehouse))
            audit(c, user["id"], "user.create", "user", row["id"], after={"role": data.role})
            return clean(row)
    except UniqueViolation:
        error("DUPLICATE", "Пользователь уже существует", 409)


@router.get("/users", tags=["Admin"])
def users(user=Depends(require("admin"))):
    with pool.connection() as c:
        return clean(all_rows(c, "SELECT id,email,full_name,role,active FROM users ORDER BY full_name"))


@router.post("/products", status_code=201, tags=["Catalog"])
def add_product(data: ProductIn, user=Depends(require("admin"))):
    if data.pickPolicy not in {"FIFO", "FEFO"}:
        error("PICK_POLICY", "Допустимы FIFO или FEFO")
    try:
        with pool.connection() as c, c.transaction():
            row = one(c, """
              INSERT INTO products(sku,name,unit,barcode,category,pick_policy,supplier_id)
              VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
            """, (data.sku.strip(), data.name.strip(), data.unit, data.barcode,
                  data.category, data.pickPolicy, data.supplierId))
            audit(c, user["id"], "product.create", "product", row["id"],
                  after={"sku": row["sku"]})
            return clean(row)
    except UniqueViolation:
        error("DUPLICATE", "SKU или штрихкод уже существует", 409)


@router.get("/products", tags=["Catalog"])
def products(q: str = "", active: bool | None = None, page: int = 1,
             limit: int = 30, user=Depends(current_user)):
    page, limit = max(1, page), max(1, min(100, limit))
    with pool.connection() as c:
        rows = all_rows(c, """
          SELECT p.id,p.sku,p.name,p.unit,p.barcode,p.category,p.pick_policy,p.active,
                 count(*) OVER()::int AS total
          FROM products p
          WHERE (%s='' OR p.sku ILIKE %s OR p.name ILIKE %s OR p.barcode ILIKE %s)
            AND (%s::boolean IS NULL OR active=%s::boolean)
          ORDER BY p.sku LIMIT %s OFFSET %s
        """, (q, f"%{q}%", f"%{q}%", f"%{q}%", active, active, limit, (page - 1) * limit))
        return {"items": clean(rows), "total": rows[0]["total"] if rows else 0,
                "page": page, "limit": limit}


@router.post("/products/{product_id}/archive", tags=["Catalog"])
def archive_product(product_id: str, user=Depends(require("admin"))):
    with pool.connection() as c, c.transaction():
        row = require_row(c, "products", product_id)
        c.execute("UPDATE products SET active=false WHERE id=%s", (product_id,))
        audit(c, user["id"], "product.archive", "product", product_id,
              before={"active": row["active"]}, after={"active": False})
        return {"id": product_id, "active": False}


@router.post("/warehouses", status_code=201, tags=["Catalog"])
def add_warehouse(data: WarehouseIn, user=Depends(require("admin"))):
    try:
        with pool.connection() as c, c.transaction():
            row = one(c, "INSERT INTO warehouses(code,name) VALUES(%s,%s) RETURNING *",
                      (data.code, data.name))
            audit(c, user["id"], "warehouse.create", "warehouse", row["id"],
                  after={"code": data.code})
            return clean(row)
    except UniqueViolation:
        error("DUPLICATE", "Код склада занят", 409)


@router.get("/warehouses", tags=["Catalog"])
def warehouses(user=Depends(current_user)):
    with pool.connection() as c:
        rows = all_rows(c, """
          SELECT id,code,name,active FROM warehouses
          WHERE %s OR id=ANY(%s::uuid[]) ORDER BY code
        """, (user["role"] == "admin", user["warehouses"]))
        return clean(rows)



@router.get("/zones", tags=["Catalog"])
def zones(warehouseId: str, user=Depends(current_user)):
    warehouse_access(user, warehouseId)
    with pool.connection() as c:
        return clean(all_rows(c, """
          SELECT id,warehouse_id,code,name,active FROM zones
          WHERE warehouse_id=%s ORDER BY code
        """, (warehouseId,)))


@router.get("/suppliers", tags=["Catalog"])
def suppliers(user=Depends(current_user)):
    with pool.connection() as c:
        return clean(all_rows(c, "SELECT id,name,contact FROM suppliers ORDER BY name LIMIT 500"))


@router.post("/suppliers", status_code=201, tags=["Catalog"])
def create_supplier(data: dict, user=Depends(require("admin"))):
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 250:
        error("VALIDATION_ERROR", "Укажите название поставщика")
    with pool.connection() as c, c.transaction():
        row = one(c, "INSERT INTO suppliers(name,contact) VALUES(%s,%s) RETURNING *",
                  (name, data.get("contact")))
        audit(c, user["id"], "supplier.create", "supplier", row["id"],
              after={"name": name})
        return clean(row)


@router.get("/customers", tags=["Catalog"])
def customers(user=Depends(require("admin", "manager", "supervisor"))):
    with pool.connection() as c:
        return clean(all_rows(c, "SELECT id,name FROM customers ORDER BY name LIMIT 500"))


@router.post("/customers", status_code=201, tags=["Catalog"])
def create_customer(data: dict, user=Depends(require("admin", "manager"))):
    name = str(data.get("name", "")).strip()
    if not name or len(name) > 250:
        error("VALIDATION_ERROR", "Укажите название клиента")
    with pool.connection() as c, c.transaction():
        row = one(c, "INSERT INTO customers(name) VALUES(%s) RETURNING *", (name,))
        audit(c, user["id"], "customer.create", "customer", row["id"],
              after={"name": name})
        return clean(row)


@router.post("/zones", status_code=201, tags=["Catalog"])
def add_zone(data: ZoneIn, user=Depends(require("admin"))):
    with pool.connection() as c, c.transaction():
        require_row(c, "warehouses", data.warehouseId)
        row = one(c, "INSERT INTO zones(warehouse_id,code,name) VALUES(%s,%s,%s) RETURNING *",
                  (data.warehouseId, data.code, data.name))
        audit(c, user["id"], "zone.create", "zone", row["id"], warehouse=data.warehouseId)
        return clean(row)


@router.post("/bins", status_code=201, tags=["Catalog"])
def add_bin(data: BinIn, user=Depends(require("admin"))):
    with pool.connection() as c, c.transaction():
        zone = require_row(c, "zones", data.zoneId)
        row = one(c, """
          INSERT INTO bins(warehouse_id,zone_id,code) VALUES(%s,%s,%s) RETURNING *
        """, (zone["warehouse_id"], data.zoneId, data.code))
        audit(c, user["id"], "bin.create", "bin", row["id"], warehouse=zone["warehouse_id"])
        return clean(row)


@router.get("/bins", tags=["Catalog"])
def bins(warehouseId: str, user=Depends(current_user)):
    warehouse_access(user, warehouseId)
    with pool.connection() as c:
        return clean(all_rows(c, """
          SELECT b.id,b.code,b.zone_id,b.warehouse_id,b.active,z.code AS zone_code
          FROM bins b JOIN zones z ON z.id=b.zone_id
          WHERE b.warehouse_id=%s ORDER BY z.code,b.code
        """, (warehouseId,)))


@router.post("/lots", status_code=201, tags=["Catalog"])
def add_lot(data: LotIn, user=Depends(require("admin", "supervisor"))):
    with pool.connection() as c, c.transaction():
        require_row(c, "products", data.productId)
        row = one(c, """
          INSERT INTO lots(product_id,code,expires_at) VALUES(%s,%s,%s)
          ON CONFLICT(product_id,code) DO UPDATE SET code=excluded.code
          RETURNING *
        """, (data.productId, data.code, data.expiresAt))
        audit(c, user["id"], "lot.ensure", "lot", row["id"],
              after={"code": data.code})
        return clean(row)


@router.get("/lots", tags=["Catalog"])
def lots(productId: str, user=Depends(current_user)):
    with pool.connection() as c:
        return clean(all_rows(c, """
          SELECT * FROM lots WHERE product_id=%s ORDER BY created_at
        """, (productId,)))


@router.get("/stocks", tags=["Stocks"])
def stocks(sku: str = "", warehouseId: str | None = None,
           page: int = 1, limit: int = 50, user=Depends(current_user)):
    if warehouseId:
        warehouse_access(user, warehouseId)
    page, limit = max(1, page), max(1, min(100, limit))
    with pool.connection() as c:
        guards = all_rows(c, """
          SELECT sg.product_id,sg.warehouse_id,p.sku,p.name,w.name AS warehouse,
                 count(*) OVER()::int AS total
          FROM stock_guards sg
          JOIN products p ON p.id=sg.product_id
          JOIN warehouses w ON w.id=sg.warehouse_id
          WHERE (%s='' OR p.sku ILIKE %s OR p.name ILIKE %s)
            AND (%s::uuid IS NULL OR sg.warehouse_id=%s::uuid)
            AND (%s OR sg.warehouse_id=ANY(%s::uuid[]))
          ORDER BY p.sku,w.code LIMIT %s OFFSET %s
        """, (sku, f"%{sku}%", f"%{sku}%", warehouseId, warehouseId,
              user["role"] == "admin", user["warehouses"], limit, (page - 1) * limit))
        items = []
        for row in guards:
            qty = stock_total(c, row["product_id"], row["warehouse_id"])
            items.append({**clean(row), **qty, "available": qty["physical"] - qty["reserved"] - qty["blocked"]})
        return {"items": items, "total": guards[0]["total"] if guards else 0,
                "page": page, "limit": limit}


@router.get("/stocks/locations", tags=["Stocks"])
def stock_locations(productId: str, warehouseId: str, user=Depends(current_user)):
    warehouse_access(user, warehouseId)
    with pool.connection() as c:
        product = require_row(c, "products", productId)
        return clean(locations(c, productId, warehouseId, product["pick_policy"]))


@router.get("/audit", tags=["Audit"])
def audit_events(after: int = 0, limit: int = 50, entityId: str | None = None,
                 user=Depends(require("admin", "supervisor", "director"))):
    limit = max(1, min(100, limit))
    with pool.connection() as c:
        rows = all_rows(c, """
          SELECT a.id,a.actor_id,u.full_name AS actor,a.action,a.entity_type,
                 a.entity_id,a.warehouse_id,a.before,a.after,a.request_id,a.created_at
          FROM audit_events a LEFT JOIN users u ON u.id=a.actor_id
          WHERE a.id>%s AND (%s::text IS NULL OR a.entity_id=%s)
          AND (%s OR a.warehouse_id=ANY(%s::uuid[]))
          ORDER BY a.id ASC LIMIT %s
        """, (after, entityId, entityId,
              user["role"] in ("admin", "director"), user["warehouses"], limit))
        return {"items": clean(rows), "nextCursor": rows[-1]["id"] if rows else None}


@router.get("/dashboard", tags=["Reports"])
def dashboard(user=Depends(require("admin", "manager", "supervisor", "director"))):
    with pool.connection() as c:
        accessible = user["warehouses"]
        unlimited = user["role"] in ("admin", "director")
        orders = one(c, """
          SELECT count(DISTINCT o.id)::int AS count FROM orders o
          JOIN order_lines l ON l.order_id=o.id
          WHERE o.status NOT IN ('shipped','cancelled')
            AND (%s OR l.warehouse_id=ANY(%s::uuid[]))
        """, (unlimited, accessible))["count"]
        pending = one(c, """
          SELECT count(*)::int AS count FROM pick_tasks
          WHERE status IN ('pending','shortage_pending')
          AND (%s OR warehouse_id=ANY(%s::uuid[]))
        """, (unlimited, accessible))["count"]
        overdue = one(c, """
          SELECT count(*)::int AS count FROM reservations
          WHERE status='active' AND expires_at<now()
          AND (%s OR warehouse_id=ANY(%s::uuid[]))
        """, (unlimited, accessible))["count"]
        shortages = one(c, """
          SELECT count(*)::int AS count FROM pick_tasks
          WHERE actual_qty<expected_qty AND actual_qty IS NOT NULL
          AND (%s OR warehouse_id=ANY(%s::uuid[]))
        """, (unlimited, accessible))["count"]
        average = one(c, """
          SELECT ROUND(avg(extract(epoch FROM (completed_at-r.created_at))/60)::numeric,1)
                 AS minutes FROM pick_tasks t JOIN reservations r ON r.id=t.reservation_id
          WHERE t.completed_at IS NOT NULL
          AND (%s OR t.warehouse_id=ANY(%s::uuid[]))
        """, (unlimited, accessible))["minutes"]
        return {"openOrders": orders, "pendingPicks": pending, "overdueReservations": overdue,
                "shortages": shortages, "averagePickMinutes": float(average or 0)}


def parse_csv(c, content):
    reader = csv.DictReader(io.StringIO(content.lstrip("\ufeff")))
    expected = {"sku", "name", "unit", "barcode", "category", "pick_policy"}
    if not reader.fieldnames or not {"sku", "name"}.issubset(set(reader.fieldnames)):
        error("CSV_HEADERS", "Обязательные столбцы: sku, name")
    items = []
    errors = []
    seen = set()
    seen_barcodes = set()
    for number, row in enumerate(reader, start=2):
        if number > 12002:
            error("CSV_LIMIT", "Не более 12 000 строк за импорт")
        sku, name = (row.get("sku") or "").strip(), (row.get("name") or "").strip()
        if not sku or not name or len(sku) > 64 or len(name) > 250:
            errors.append({"row": number, "message": "Некорректные SKU/название"})
            continue
        if sku in seen:
            errors.append({"row": number, "message": "Дублирующийся SKU в файле"})
        seen.add(sku)
        policy = (row.get("pick_policy") or "FIFO").strip()
        if policy not in ("FIFO", "FEFO"):
            errors.append({"row": number, "message": "Неизвестная политика отбора"})
        barcode = (row.get("barcode") or "").strip() or None
        if barcode:
            if barcode in seen_barcodes:
                errors.append({"row": number, "message": "Дублирующийся штрихкод в файле"})
            seen_barcodes.add(barcode)
        unit = (row.get("unit") or "шт").strip()
        if not unit or len(unit) > 30 or len(barcode or "") > 100:
            errors.append({"row": number, "message": "Некорректная единица или штрихкод"})
        items.append({"row": number, "sku": sku, "name": name,
                      "unit": unit, "barcode": barcode,
                      "category": (row.get("category") or "Общее").strip(),
                      "pick_policy": policy})
    if items:
        db_rows = all_rows(c, "SELECT sku FROM products WHERE sku=ANY(%s::text[])",
                           ([item["sku"] for item in items],))
        existing = {r["sku"] for r in db_rows}
        errors.extend({"row": item["row"], "message": "SKU уже существует"}
                      for item in items if item["sku"] in existing)
        barcodes = [item["barcode"] for item in items if item["barcode"]]
        if barcodes:
            existing_codes = {r["barcode"] for r in all_rows(
                c, "SELECT barcode FROM products WHERE barcode=ANY(%s::text[])",
                (barcodes,)
            )}
            errors.extend({"row": item["row"], "message": "Штрихкод уже существует"}
                          for item in items if item["barcode"] in existing_codes)
    return {"items": items, "errors": errors, "valid": not errors,
            "columns": list(reader.fieldnames or [])}


@router.post("/import/products/preview", tags=["CSV"])
def preview(data: CsvPayload, user=Depends(require("admin"))):
    with pool.connection() as c:
        result = parse_csv(c, data.content)
        return {"count": len(result["items"]), "preview": result["items"][:20],
                "errors": result["errors"], "valid": result["valid"]}


@router.post("/import/products/commit", tags=["CSV"])
def import_products(data: CsvPayload, user=Depends(require("admin"))):
    with pool.connection() as c, c.transaction():
        result = parse_csv(c, data.content)
        if not result["valid"]:
            error("CSV_INVALID", "Исправьте ошибки перед импортом", 409, result["errors"])
        for item in result["items"]:
            c.execute("""
              INSERT INTO products(sku,name,unit,barcode,category,pick_policy)
              VALUES(%s,%s,%s,%s,%s,%s)
            """, (item["sku"], item["name"], item["unit"], item["barcode"],
                  item["category"], item["pick_policy"]))
        audit(c, user["id"], "products.import", "import", "csv",
              after={"count": len(result["items"])})
        return {"created": len(result["items"])}
