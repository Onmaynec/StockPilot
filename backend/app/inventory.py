"""Two-person cycle counts, frozen snapshots and atomic variance approval."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .auth import require, warehouse_access
from .common import check_bin, check_lot, clean, require_row
from .db import all_rows, audit, error, location_available, lock_stock, movement, one, outbox, pool

router = APIRouter()


class CountCreate(BaseModel):
    zoneId: str


class CountLine(BaseModel):
    productId: str
    binId: str
    lotId: str
    actualQty: int = Field(ge=0)


class CountResults(BaseModel):
    items: list[CountLine] = Field(min_length=1)


@router.post("/inventory", status_code=201, tags=["Inventory"])
def create_count(data: CountCreate, user=Depends(require("admin", "supervisor"))):
    with pool.connection() as c, c.transaction():
        zone = require_row(c, "zones", data.zoneId)
        warehouse_access(user, zone["warehouse_id"])
        if not zone["active"]:
            error("INACTIVE_ZONE", "Зона архивирована", 409)
        # Lock all existing SKU guards in zone before freezing the physical snapshot.
        affected = all_rows(c, """
          SELECT DISTINCT m.product_id,m.warehouse_id
          FROM stock_movements m JOIN bins b ON b.id=m.bin_id
          WHERE b.zone_id=%s
        """, (data.zoneId,))
        lock_stock(c, [(r["product_id"], r["warehouse_id"]) for r in affected])
        record = one(c, """
          INSERT INTO inventory_counts(zone_id,warehouse_id,created_by)
          VALUES(%s,%s,%s) RETURNING *
        """, (data.zoneId, zone["warehouse_id"], user["id"]))
        snapshot = all_rows(c, """
          SELECT m.product_id,m.bin_id,m.lot_id,sum(m.qty)::int AS qty
          FROM stock_movements m JOIN bins b ON b.id=m.bin_id
          WHERE b.zone_id=%s GROUP BY m.product_id,m.bin_id,m.lot_id
          HAVING sum(m.qty)<>0
        """, (data.zoneId,))
        for line in snapshot:
            c.execute("""
              INSERT INTO inventory_lines(count_id,product_id,bin_id,lot_id,snapshot_qty)
              VALUES(%s,%s,%s,%s,%s)
            """, (record["id"], line["product_id"], line["bin_id"],
                  line["lot_id"], line["qty"]))
        audit(c, user["id"], "inventory.create", "inventory", record["id"],
              after={"snapshotLines": len(snapshot)}, warehouse=zone["warehouse_id"])
        return {"id": str(record["id"]), "status": "counting",
                "snapshotLines": len(snapshot)}


@router.get("/inventory", tags=["Inventory"])
def list_counts(user=Depends(require("admin", "picker", "supervisor", "director"))):
    with pool.connection() as c:
        return clean(all_rows(c, """
          SELECT i.* ,z.code AS zone_code FROM inventory_counts i
          JOIN zones z ON z.id=i.zone_id
          WHERE %s OR i.warehouse_id=ANY(%s::uuid[])
          ORDER BY i.created_at DESC LIMIT 100
        """, (user["role"] in ("admin", "director"), user["warehouses"])))


@router.get("/inventory/{count_id}", tags=["Inventory"])
def get_count(count_id: str, user=Depends(require("admin", "picker", "supervisor", "director"))):
    with pool.connection() as c:
        count = require_row(c, "inventory_counts", count_id)
        if user["role"] != "director":
            warehouse_access(user, count["warehouse_id"])
        lines = all_rows(c, """
          SELECT l.*,p.sku,b.code AS bin_code,lot.code AS lot_code
          FROM inventory_lines l JOIN products p ON p.id=l.product_id
          JOIN bins b ON b.id=l.bin_id JOIN lots lot ON lot.id=l.lot_id
          WHERE l.count_id=%s ORDER BY p.sku,b.code
        """, (count_id,))
        return {"count": clean(count), "items": clean(lines)}


@router.post("/inventory/{count_id}/count", tags=["Inventory"])
def enter_count(count_id: str, data: CountResults,
                user=Depends(require("admin", "picker", "supervisor"))):
    with pool.connection() as c, c.transaction():
        record = one(c, "SELECT * FROM inventory_counts WHERE id=%s FOR UPDATE", (count_id,))
        if not record:
            error("NOT_FOUND", "Инвентаризация не найдена", 404)
        warehouse_access(user, record["warehouse_id"])
        if record["status"] != "counting":
            error("COUNT_STATE", "Пересчёт уже закрыт", 409)
        if record["counted_by"] and str(record["counted_by"]) != str(user["id"]):
            error("COUNT_ASSIGNEE", "Пересчёт уже начал другой исполнитель", 409)
        keys = set()
        for item in data.items:
            bin_row = check_bin(c, item.binId, user)
            if str(bin_row["zone_id"]) != str(record["zone_id"]):
                error("ZONE_MISMATCH", "Ячейка не входит в зону", 409)
            check_lot(c, item.lotId, item.productId)
            key = (item.productId, item.binId, item.lotId)
            if key in keys:
                error("DUPLICATE_LINE", "Одна позиция передана дважды")
            keys.add(key)
        lock_stock(c, [(item.productId, record["warehouse_id"]) for item in data.items])
        for item in data.items:
            # New product/bin/lot combination: snapshot is the CURRENT physical qty.
            # If movement appeared after initial snapshot, approval detects conflict
            # through the frozen-quantity and movement timestamp check.
            existing = one(c, """
              SELECT snapshot_qty FROM inventory_lines
              WHERE count_id=%s AND product_id=%s AND bin_id=%s AND lot_id=%s
            """, (count_id, item.productId, item.binId, item.lotId))
            if existing:
                c.execute("""
                  UPDATE inventory_lines SET actual_qty=%s
                  WHERE count_id=%s AND product_id=%s AND bin_id=%s AND lot_id=%s
                """, (item.actualQty, count_id, item.productId, item.binId, item.lotId))
            else:
                physical = one(c, """
                  SELECT COALESCE(sum(qty),0)::int AS qty FROM stock_movements
                  WHERE product_id=%s AND warehouse_id=%s AND bin_id=%s AND lot_id=%s
                """, (item.productId, record["warehouse_id"], item.binId, item.lotId))["qty"]
                if physical != 0:
                    error("SNAPSHOT_CHANGED", "Новая строка имеет движение после среза", 409)
                c.execute("""
                  INSERT INTO inventory_lines(count_id,product_id,bin_id,lot_id,snapshot_qty,actual_qty)
                  VALUES(%s,%s,%s,%s,0,%s)
                """, (count_id, item.productId, item.binId, item.lotId, item.actualQty))
        c.execute("UPDATE inventory_counts SET counted_by=%s WHERE id=%s", (user["id"], count_id))
        audit(c, user["id"], "inventory.count", "inventory", count_id,
              after={"linesSubmitted": len(data.items)}, warehouse=record["warehouse_id"])
        return {"id": count_id, "updated": len(data.items)}


@router.post("/inventory/{count_id}/submit", tags=["Inventory"])
def submit_count(count_id: str, user=Depends(require("admin", "picker", "supervisor"))):
    with pool.connection() as c, c.transaction():
        count = one(c, "SELECT * FROM inventory_counts WHERE id=%s FOR UPDATE", (count_id,))
        if not count:
            error("NOT_FOUND", "Инвентаризация не найдена", 404)
        warehouse_access(user, count["warehouse_id"])
        if count["status"] != "counting" or not count["counted_by"]:
            error("COUNT_STATE", "Пересчёт не заполнен", 409)
        if str(count["counted_by"]) != str(user["id"]) and user["role"] != "admin":
            error("COUNT_ASSIGNEE", "Подтверждать факт может исполнитель", 403)
        if one(c, "SELECT 1 FROM inventory_lines WHERE count_id=%s AND actual_qty IS NULL",
               (count_id,)):
            error("INCOMPLETE_COUNT", "Не все строки пересчитаны", 409)
        c.execute("UPDATE inventory_counts SET status='submitted' WHERE id=%s", (count_id,))
        audit(c, user["id"], "inventory.submit", "inventory", count_id,
              after={"status": "submitted"}, warehouse=count["warehouse_id"])
        return {"id": count_id, "status": "submitted"}


@router.post("/inventory/{count_id}/approve", tags=["Inventory"])
def approve_count(count_id: str, user=Depends(require("admin", "supervisor"))):
    with pool.connection() as c, c.transaction():
        count = one(c, "SELECT * FROM inventory_counts WHERE id=%s FOR UPDATE", (count_id,))
        if not count:
            error("NOT_FOUND", "Инвентаризация не найдена", 404)
        warehouse_access(user, count["warehouse_id"])
        if count["status"] != "submitted":
            error("COUNT_STATE", "Акт не передан на утверждение", 409)
        if str(count["counted_by"]) == str(user["id"]):
            error("SEPARATION_OF_DUTIES", "Исполнитель не утверждает свой пересчёт", 403)
        lines = all_rows(c, """
          SELECT * FROM inventory_lines WHERE count_id=%s ORDER BY product_id,bin_id,lot_id
        """, (count_id,))
        lock_stock(c, [(line["product_id"], count["warehouse_id"]) for line in lines])
        # Any change after the frozen snapshot (including a newly arrived lot)
        # invalidates approval; otherwise a count could erase a legitimate receipt.
        if one(c, """
          SELECT 1 FROM stock_movements m JOIN bins b ON b.id=m.bin_id
          WHERE b.zone_id=%s AND m.created_at>%s LIMIT 1
        """, (count["zone_id"], count["created_at"])):
            error("STALE_SNAPSHOT", "После начала пересчёта в зоне были движения", 409)
        deltas = []
        for line in lines:
            current = one(c, """
              SELECT COALESCE(sum(qty),0)::int AS qty FROM stock_movements
              WHERE product_id=%s AND warehouse_id=%s AND bin_id=%s AND lot_id=%s
            """, (line["product_id"], count["warehouse_id"], line["bin_id"],
                  line["lot_id"]))["qty"]
            if current != line["snapshot_qty"]:
                error("STALE_SNAPSHOT", "Остаток изменился после начала инвентаризации", 409, {
                    "binId": str(line["bin_id"]), "snapshot": line["snapshot_qty"],
                    "current": current,
                })
            delta = line["actual_qty"] - line["snapshot_qty"]
            if delta < 0 and location_available(c, line["product_id"],
                                                 count["warehouse_id"], line["bin_id"],
                                                 line["lot_id"]) + delta < 0:
                error("RESERVED_STOCK", "Корректировка затрагивает активный резерв", 409)
            deltas.append((line, delta))
        for line, delta in deltas:
            movement(c, line["product_id"], count["warehouse_id"], line["bin_id"],
                     line["lot_id"], delta, "inventory", count_id, user["id"])
        c.execute("""
          UPDATE inventory_counts SET status='approved',approved_by=%s,approved_at=now()
          WHERE id=%s
        """, (user["id"], count_id))
        result = {"id": count_id, "status": "approved",
                  "adjustments": sum(1 for _, d in deltas if d),
                  "deltaQty": sum(d for _, d in deltas)}
        audit(c, user["id"], "inventory.approve", "inventory", count_id,
              after=result, warehouse=count["warehouse_id"])
        outbox(c, "inventory.approved", count_id, result)
        return result
