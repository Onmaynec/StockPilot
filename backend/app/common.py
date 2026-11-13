"""Validation and authorization helpers shared by API modules."""
from fastapi.encoders import jsonable_encoder
from psycopg.errors import ForeignKeyViolation, UniqueViolation

from .auth import warehouse_access
from .db import error, one


def require_row(c, table, row_id):
    allowed = {
        "products", "warehouses", "zones", "bins", "lots", "orders",
        "receipts", "transfers", "shipments", "reservations", "pick_tasks",
        "inventory_counts", "suppliers", "users",
    }
    if table not in allowed:
        raise RuntimeError("Invalid table")
    row = one(c, f"SELECT * FROM {table} WHERE id=%s", (row_id,))
    if not row:
        error("NOT_FOUND", "Запись не найдена", 404)
    return row


def check_bin(c, bin_id, user=None):
    row = one(c, """
      SELECT b.*, z.active AS zone_active, w.active AS warehouse_active
      FROM bins b JOIN zones z ON z.id=b.zone_id
      JOIN warehouses w ON w.id=b.warehouse_id WHERE b.id=%s
    """, (bin_id,))
    if not row:
        error("BIN_NOT_FOUND", "Ячейка не найдена", 404)
    if not (row["active"] and row["zone_active"] and row["warehouse_active"]):
        error("INACTIVE_BIN", "Ячейка или склад архивированы", 409)
    if user:
        warehouse_access(user, row["warehouse_id"])
    return row


def check_lot(c, lot_id, product_id):
    lot = require_row(c, "lots", lot_id)
    if str(lot["product_id"]) != str(product_id):
        error("LOT_MISMATCH", "Партия не принадлежит товару", 400)
    return lot


def order_access(c, order_id, user):
    order = require_row(c, "orders", order_id)
    if user["role"] != "admin":
        warehouses = c.execute(
            "SELECT DISTINCT warehouse_id FROM order_lines WHERE order_id=%s", (order_id,)
        ).fetchall()
        for row in warehouses:
            warehouse_access(user, row["warehouse_id"])
    return order


def clean(row):
    return jsonable_encoder(row)


def integrity_error(exc):
    if isinstance(exc, (UniqueViolation, ForeignKeyViolation)):
        error("CONSTRAINT", "Дублирующийся ключ или неверная ссылка", 409)
    raise exc
