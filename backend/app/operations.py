"""Transactional receiving, reservations, picking, shipping, returns and transfers."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Header
from pydantic import BaseModel, Field

from .auth import current_user, require, warehouse_access
from .common import check_bin, check_lot, clean, order_access, require_row
from .db import (
    all_rows, audit, error, location_available, locations, lock_stock, movement,
    one, outbox, pool, replay, save_replay, stock_total,
)

router = APIRouter()


class ReceiptItem(BaseModel):
    productId: str
    binId: str
    lotId: str
    plannedQty: int = Field(gt=0)


class ReceiptInput(BaseModel):
    supplierId: str | None = None
    items: list[ReceiptItem] = Field(min_length=1)


class ActualItem(BaseModel):
    lineId: str
    actualQty: int = Field(ge=0)
    reason: str = ""


class ReceiptActual(BaseModel):
    items: list[ActualItem] = Field(min_length=1)


class TransferInput(BaseModel):
    productId: str
    lotId: str
    fromBinId: str
    toBinId: str
    qty: int = Field(gt=0)


class OrderItem(BaseModel):
    productId: str
    warehouseId: str
    qty: int = Field(gt=0)


class OrderInput(BaseModel):
    customer: str = Field(min_length=1, max_length=250)
    items: list[OrderItem] = Field(min_length=1)


class ReserveInput(BaseModel):
    ttlMinutes: int = Field(default=60, ge=5, le=1440)


class PickInput(BaseModel):
    barcode: str = Field(min_length=1)
    actualQty: int = Field(ge=0)


class AssignInput(BaseModel):
    userId: str


class ReturnInput(BaseModel):
    reservationId: str
    binId: str
    qty: int = Field(gt=0)


class BlockInput(BaseModel):
    productId: str
    binId: str
    lotId: str
    qty: int = Field(gt=0)
    reason: str = Field(min_length=3)


@router.post("/receipts", status_code=201, tags=["Receiving"])
def create_receipt(data: ReceiptInput, user=Depends(require("admin", "manager", "supervisor"))):
    with pool.connection() as c, c.transaction():
        row = one(c, "INSERT INTO receipts(supplier_id,created_by) VALUES(%s,%s) RETURNING *",
                  (data.supplierId, user["id"]))
        for item in data.items:
            product = require_row(c, "products", item.productId)
            if not product["active"]:
                error("INACTIVE_PRODUCT", "Архивный товар недоступен", 409)
            bin_row = check_bin(c, item.binId, user)
            check_lot(c, item.lotId, item.productId)
            c.execute("""
              INSERT INTO receipt_lines(receipt_id,product_id,bin_id,lot_id,planned_qty)
              VALUES (%s,%s,%s,%s,%s)
            """, (row["id"], item.productId, item.binId, item.lotId, item.plannedQty))
        audit(c, user["id"], "receipt.create", "receipt", row["id"],
              after={"items": len(data.items)})
        return clean(row)


@router.get("/receipts", tags=["Receiving"])
def receipts(user=Depends(require("admin", "manager", "picker", "supervisor"))):
    with pool.connection() as c:
        rows = all_rows(c, """
          SELECT DISTINCT r.* FROM receipts r JOIN receipt_lines l ON l.receipt_id=r.id
          JOIN bins b ON b.id=l.bin_id
          WHERE %s OR b.warehouse_id=ANY(%s::uuid[])
          ORDER BY r.created_at DESC LIMIT 100
        """, (user["role"] == "admin", user["warehouses"]))
        return clean(rows)


@router.get("/receipts/{receipt_id}", tags=["Receiving"])
def get_receipt(receipt_id: str, user=Depends(require("admin", "manager", "picker", "supervisor"))):
    with pool.connection() as c:
        receipt = require_row(c, "receipts", receipt_id)
        lines = all_rows(c, """
          SELECT l.*,p.sku,b.warehouse_id FROM receipt_lines l
          JOIN products p ON p.id=l.product_id JOIN bins b ON b.id=l.bin_id
          WHERE l.receipt_id=%s ORDER BY p.sku
        """, (receipt_id,))
        for line in lines:
            warehouse_access(user, line["warehouse_id"])
        return {"receipt": clean(receipt), "items": clean(lines)}


@router.post("/receipts/{receipt_id}/confirm", tags=["Receiving"])
def confirm_receipt(receipt_id: str, data: ReceiptActual,
                    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                    user=Depends(require("admin", "picker", "supervisor"))):
    with pool.connection() as c, c.transaction():
        permitted = all_rows(c, """
          SELECT DISTINCT b.warehouse_id FROM receipt_lines l
          JOIN bins b ON b.id=l.bin_id WHERE l.receipt_id=%s
        """, (receipt_id,))
        for item in permitted:
            warehouse_access(user, item["warehouse_id"])
        scope = "receipt:" + receipt_id
        digest, previous = replay(c, scope, idempotency_key, data.model_dump())
        if previous is not None:
            return previous
        receipt = one(c, "SELECT * FROM receipts WHERE id=%s FOR UPDATE", (receipt_id,))
        if not receipt:
            error("NOT_FOUND", "Приёмка не найдена", 404)
        if receipt["status"] != "draft":
            error("ALREADY_POSTED", "Документ уже проведён", 409)
        lines = all_rows(c, """
          SELECT l.*,b.warehouse_id FROM receipt_lines l
          JOIN bins b ON b.id=l.bin_id WHERE l.receipt_id=%s
        """, (receipt_id,))
        incoming = {item.lineId: item for item in data.items}
        if len(incoming) != len(data.items) or set(incoming) != {str(row["id"]) for row in lines}:
            error("LINES_MISMATCH", "Передайте факт по каждой строке документа")
        lock_stock(c, [(r["product_id"], r["warehouse_id"]) for r in lines])
        discrepancies = []
        for line in lines:
            item = incoming[str(line["id"])]
            check_bin(c, line["bin_id"], user)
            if item.actualQty != line["planned_qty"] and not item.reason.strip():
                error("REASON_REQUIRED", "Расхождение требует указания причины")
            c.execute("""
              UPDATE receipt_lines SET actual_qty=%s,reason=%s WHERE id=%s
            """, (item.actualQty, item.reason or None, line["id"]))
            movement(c, line["product_id"], line["warehouse_id"], line["bin_id"],
                     line["lot_id"], item.actualQty, "receipt", receipt_id, user["id"])
            difference = line["planned_qty"] - item.actualQty
            if difference:
                discrepancies.append({"lineId": str(line["id"]), "difference": difference,
                                      "reason": item.reason})
        c.execute("""
          UPDATE receipts SET status='posted',confirmed_by=%s,posted_at=now()
          WHERE id=%s
        """, (user["id"], receipt_id))
        outcome = {"id": receipt_id, "status": "posted", "discrepancies": discrepancies}
        audit(c, user["id"], "receipt.post", "receipt", receipt_id,
              before={"status": "draft"}, after=outcome)
        outbox(c, "receipt.posted", receipt_id, outcome)
        save_replay(c, scope, idempotency_key, digest, outcome)
        return outcome


@router.post("/transfers", status_code=201, tags=["Transfers"])
def create_transfer(data: TransferInput, user=Depends(require("admin", "picker", "supervisor"))):
    with pool.connection() as c, c.transaction():
        from_bin = check_bin(c, data.fromBinId, user)
        check_bin(c, data.toBinId, user)
        check_lot(c, data.lotId, data.productId)
        if data.fromBinId == data.toBinId:
            error("SAME_BIN", "Исходная и целевая ячейки совпадают")
        row = one(c, """
          INSERT INTO transfers(product_id,lot_id,from_bin_id,to_bin_id,qty,actor_id)
          VALUES(%s,%s,%s,%s,%s,%s) RETURNING *
        """, (data.productId, data.lotId, data.fromBinId, data.toBinId, data.qty, user["id"]))
        audit(c, user["id"], "transfer.create", "transfer", row["id"],
              warehouse=from_bin["warehouse_id"])
        return clean(row)


@router.get("/transfers", tags=["Transfers"])
def transfers(user=Depends(require("admin", "picker", "supervisor"))):
    with pool.connection() as c:
        return clean(all_rows(c, """
          SELECT t.* FROM transfers t JOIN bins b ON b.id=t.from_bin_id
          WHERE %s OR b.warehouse_id=ANY(%s::uuid[])
          ORDER BY t.id DESC LIMIT 100
        """, (user["role"] == "admin", user["warehouses"])))


@router.post("/transfers/{transfer_id}/confirm", tags=["Transfers"])
def confirm_transfer(transfer_id: str,
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                     user=Depends(require("admin", "picker", "supervisor"))):
    with pool.connection() as c, c.transaction():
        sides = one(c, """
          SELECT a.warehouse_id AS source_wh, b.warehouse_id AS target_wh
          FROM transfers t JOIN bins a ON a.id=t.from_bin_id
          JOIN bins b ON b.id=t.to_bin_id WHERE t.id=%s
        """, (transfer_id,))
        if not sides:
            error("NOT_FOUND", "Перемещение не найдено", 404)
        warehouse_access(user, sides["source_wh"])
        warehouse_access(user, sides["target_wh"])
        digest, previous = replay(c, "transfer:" + transfer_id, idempotency_key, {})
        if previous is not None:
            return previous
        row = one(c, "SELECT * FROM transfers WHERE id=%s FOR UPDATE", (transfer_id,))
        if not row:
            error("NOT_FOUND", "Перемещение не найдено", 404)
        if row["status"] != "draft":
            error("ALREADY_POSTED", "Перемещение уже проведено", 409)
        src = check_bin(c, row["from_bin_id"], user)
        dst = check_bin(c, row["to_bin_id"], user)
        lock_stock(c, [(row["product_id"], src["warehouse_id"]),
                       (row["product_id"], dst["warehouse_id"])])
        available = location_available(c, row["product_id"], src["warehouse_id"],
                                       row["from_bin_id"], row["lot_id"])
        if available < row["qty"]:
            error("INSUFFICIENT_STOCK", "В ячейке недостаточно свободного товара", 409,
                  {"available": available})
        movement(c, row["product_id"], src["warehouse_id"], row["from_bin_id"],
                 row["lot_id"], -row["qty"], "transfer", transfer_id, user["id"])
        movement(c, row["product_id"], dst["warehouse_id"], row["to_bin_id"],
                 row["lot_id"], row["qty"], "transfer", transfer_id, user["id"])
        c.execute("UPDATE transfers SET status='posted',posted_at=now() WHERE id=%s", (transfer_id,))
        result = {"id": transfer_id, "status": "posted", "qty": row["qty"]}
        audit(c, user["id"], "transfer.post", "transfer", transfer_id, after=result,
              warehouse=src["warehouse_id"])
        outbox(c, "transfer.posted", transfer_id, result)
        save_replay(c, "transfer:" + transfer_id, idempotency_key, digest, result)
        return result


@router.post("/orders", status_code=201, tags=["Orders"])
def create_order(data: OrderInput, user=Depends(require("admin", "manager"))):
    with pool.connection() as c, c.transaction():
        for item in data.items:
            product = require_row(c, "products", item.productId)
            if not product["active"]:
                error("INACTIVE_PRODUCT", "Архивный товар нельзя заказать", 409)
            warehouse_access(user, item.warehouseId)
            require_row(c, "warehouses", item.warehouseId)
        row = one(c, "INSERT INTO orders(customer,created_by) VALUES(%s,%s) RETURNING *",
                  (data.customer, user["id"]))
        lines = []
        for item in data.items:
            line = one(c, """
              INSERT INTO order_lines(order_id,product_id,warehouse_id,qty)
              VALUES(%s,%s,%s,%s) RETURNING *
            """, (row["id"], item.productId, item.warehouseId, item.qty))
            lines.append(clean(line))
        audit(c, user["id"], "order.create", "order", row["id"],
              after={"customer": data.customer, "items": len(lines)})
        return {"id": str(row["id"]), "status": "draft", "version": 1, "items": lines}


@router.get("/orders", tags=["Orders"])
def orders(status: str | None = None, user=Depends(require("admin", "manager", "supervisor", "picker"))):
    with pool.connection() as c:
        return clean(all_rows(c, """
          SELECT DISTINCT o.id,o.customer,o.status,o.version,o.created_at,o.updated_at
          FROM orders o JOIN order_lines l ON l.order_id=o.id
          WHERE (%s::text IS NULL OR o.status=%s)
            AND (%s OR l.warehouse_id=ANY(%s::uuid[]))
          ORDER BY o.created_at DESC LIMIT 100
        """, (status, status, user["role"] == "admin", user["warehouses"])))


@router.get("/orders/{order_id}", tags=["Orders"])
def get_order(order_id: str, user=Depends(require("admin", "manager", "supervisor", "picker"))):
    with pool.connection() as c:
        order = order_access(c, order_id, user)
        lines = all_rows(c, """
          SELECT l.*,p.sku,p.name,w.name AS warehouse FROM order_lines l
          JOIN products p ON p.id=l.product_id
          JOIN warehouses w ON w.id=l.warehouse_id WHERE order_id=%s
        """, (order_id,))
        tasks = all_rows(c, """
          SELECT t.*,p.sku,b.code AS bin_code,r.lot_id,r.qty AS reserved_qty
          FROM pick_tasks t JOIN reservations r ON r.id=t.reservation_id
          JOIN products p ON p.id=r.product_id JOIN bins b ON b.id=r.bin_id
          WHERE t.order_id=%s
        """, (order_id,))
        shipments = all_rows(c, "SELECT * FROM shipments WHERE order_id=%s", (order_id,))
        return {"order": clean(order), "items": clean(lines), "picks": clean(tasks),
                "shipments": clean(shipments)}


@router.post("/orders/{order_id}/reserve", tags=["Orders"])
def reserve_order(order_id: str, data: ReserveInput,
                  idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                  user=Depends(require("admin", "manager"))):
    with pool.connection() as c, c.transaction():
        order_access(c, order_id, user)
        digest, previous = replay(c, "reserve:" + order_id, idempotency_key,
                                  data.model_dump())
        if previous is not None:
            return previous
        order = one(c, "SELECT * FROM orders WHERE id=%s FOR UPDATE", (order_id,))
        if not order:
            error("NOT_FOUND", "Заказ не найден", 404)
        if order["status"] != "draft":
            error("ORDER_STATE", "Резервировать можно только черновик", 409)
        lines = all_rows(c, """
          SELECT l.*,p.sku,p.pick_policy,p.active FROM order_lines l
          JOIN products p ON p.id=l.product_id WHERE l.order_id=%s
          ORDER BY l.product_id,l.warehouse_id,l.id
        """, (order_id,))
        if not lines:
            error("EMPTY_ORDER", "Нельзя резервировать пустой заказ", 409)
        for line in lines:
            warehouse_access(user, line["warehouse_id"])
            if not line["active"]:
                error("INACTIVE_PRODUCT", "Архивный товар нельзя резервировать", 409)
        lock_stock(c, [(line["product_id"], line["warehouse_id"]) for line in lines])
        until = datetime.now(timezone.utc) + timedelta(minutes=data.ttlMinutes)
        reservations = []
        for line in lines:
            remaining = line["qty"]
            for loc in locations(c, line["product_id"], line["warehouse_id"],
                                 line["pick_policy"]):
                available = max(0, loc["available"])
                if not available:
                    continue
                qty = min(available, remaining)
                record = one(c, """
                  INSERT INTO reservations(order_line_id,product_id,warehouse_id,bin_id,lot_id,qty,expires_at)
                  VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id
                """, (line["id"], line["product_id"], line["warehouse_id"],
                      loc["bin_id"], loc["lot_id"], qty, until))
                c.execute("""
                  INSERT INTO pick_tasks(reservation_id,order_id,warehouse_id,expected_qty)
                  VALUES(%s,%s,%s,%s)
                """, (record["id"], order_id, line["warehouse_id"], qty))
                reservations.append({"id": str(record["id"]), "qty": qty,
                                     "sku": line["sku"], "warehouseId": str(line["warehouse_id"])})
                remaining -= qty
                if remaining == 0:
                    break
            if remaining:
                total = stock_total(c, line["product_id"], line["warehouse_id"])
                error("INSUFFICIENT_STOCK", "Недостаточно доступного остатка", 409, {
                    "sku": line["sku"], "missing": remaining,
                    "available": total["physical"] - total["reserved"] - total["blocked"],
                })
        for warehouse_id in {line["warehouse_id"] for line in lines}:
            c.execute("INSERT INTO shipments(order_id,warehouse_id) VALUES(%s,%s)",
                      (order_id, warehouse_id))
        c.execute("""
          UPDATE orders SET status='confirmed',version=version+1,updated_at=now()
          WHERE id=%s
        """, (order_id,))
        result = {"orderId": order_id, "status": "confirmed",
                  "reservations": reservations, "expiresAt": until.isoformat()}
        audit(c, user["id"], "order.reserve", "order", order_id,
              before={"status": "draft"}, after=result)
        outbox(c, "order.reserved", order_id, {"orderId": order_id})
        save_replay(c, "reserve:" + order_id, idempotency_key, digest, result)
        return result


@router.post("/orders/{order_id}/cancel", tags=["Orders"])
def cancel_order(order_id: str, user=Depends(require("admin", "manager"))):
    with pool.connection() as c, c.transaction():
        order_access(c, order_id, user)
        order = one(c, "SELECT * FROM orders WHERE id=%s FOR UPDATE", (order_id,))
        if order["status"] in ("shipped", "cancelled"):
            error("ORDER_STATE", "Заказ уже закрыт", 409)
        if one(c, "SELECT id FROM shipments WHERE order_id=%s AND status='posted'",
               (order_id,)):
            error("PARTIALLY_SHIPPED", "Заказ уже частично отгружен", 409)
        lines = all_rows(c, "SELECT product_id,warehouse_id FROM order_lines WHERE order_id=%s",
                         (order_id,))
        lock_stock(c, [(r["product_id"], r["warehouse_id"]) for r in lines])
        c.execute("""
          UPDATE reservations SET status='released' WHERE order_line_id IN
          (SELECT id FROM order_lines WHERE order_id=%s) AND status='active'
        """, (order_id,))
        c.execute("UPDATE pick_tasks SET status='cancelled' WHERE order_id=%s AND status NOT IN ('cancelled')",
                  (order_id,))
        c.execute("UPDATE orders SET status='cancelled',version=version+1,updated_at=now() WHERE id=%s",
                  (order_id,))
        audit(c, user["id"], "order.cancel", "order", order_id,
              before={"status": order["status"]}, after={"status": "cancelled"})
        return {"id": order_id, "status": "cancelled"}


@router.get("/picks", tags=["Picking"])
def picks(warehouseId: str | None = None, user=Depends(require("admin", "manager", "picker", "supervisor"))):
    if warehouseId:
        warehouse_access(user, warehouseId)
    with pool.connection() as c:
        return clean(all_rows(c, """
          SELECT t.*,r.bin_id,r.lot_id,r.expires_at,p.sku,p.name,p.barcode,
                 b.code AS bin_code,o.customer
          FROM pick_tasks t JOIN reservations r ON r.id=t.reservation_id
          JOIN products p ON p.id=r.product_id JOIN bins b ON b.id=r.bin_id
          JOIN orders o ON o.id=t.order_id
          WHERE (%s::uuid IS NULL OR t.warehouse_id=%s::uuid)
            AND (%s OR t.warehouse_id=ANY(%s::uuid[]))
          ORDER BY CASE t.status WHEN 'pending' THEN 0 WHEN 'shortage_pending' THEN 1 ELSE 2 END,
                   r.expires_at ASC LIMIT 200
        """, (warehouseId, warehouseId, user["role"] == "admin", user["warehouses"])))


@router.post("/picks/{task_id}/assign", tags=["Picking"])
def assign_pick(task_id: str, data: AssignInput,
                user=Depends(require("admin", "supervisor"))):
    with pool.connection() as c, c.transaction():
        task_ref = require_row(c, "pick_tasks", task_id)
        c.execute("SELECT id FROM orders WHERE id=%s FOR UPDATE", (task_ref["order_id"],))
        task = one(c, "SELECT * FROM pick_tasks WHERE id=%s FOR UPDATE", (task_id,))
        warehouse_access(user, task["warehouse_id"])
        if task["status"] != "pending":
            error("TASK_STATE", "Задание уже начато", 409)
        assignee = require_row(c, "users", data.userId)
        if not assignee["active"] or assignee["role"] != "picker":
            error("ASSIGNEE", "Назначить можно только действующего кладовщика")
        if not one(c, "SELECT 1 FROM user_warehouses WHERE user_id=%s AND warehouse_id=%s",
                   (data.userId, task["warehouse_id"])):
            error("WAREHOUSE_FORBIDDEN", "Исполнитель не прикреплён к складу", 403)
        c.execute("UPDATE pick_tasks SET assigned_to=%s WHERE id=%s", (data.userId, task_id))
        audit(c, user["id"], "pick.assign", "pick", task_id,
              after={"assignee": data.userId}, warehouse=task["warehouse_id"])
        return {"id": task_id, "assignedTo": data.userId}


def refresh_order_state(c, order_id):
    states = [r["status"] for r in all_rows(
        c, "SELECT status FROM pick_tasks WHERE order_id=%s", (order_id,)
    )]
    if states and all(s in ("completed", "accepted") for s in states):
        c.execute("UPDATE orders SET status='picked',updated_at=now() WHERE id=%s",
                  (order_id,))


@router.post("/picks/{task_id}/confirm", tags=["Picking"])
def confirm_pick(task_id: str, data: PickInput,
                 user=Depends(require("admin", "picker", "supervisor"))):
    with pool.connection() as c, c.transaction():
        task_ref = one(c, "SELECT order_id FROM pick_tasks WHERE id=%s", (task_id,))
        if not task_ref:
            error("NOT_FOUND", "Задание не найдено", 404)
        order = one(c, "SELECT * FROM orders WHERE id=%s FOR UPDATE", (task_ref["order_id"],))
        if order["status"] not in ("confirmed", "picking"):
            error("ORDER_STATE", "Заказ не находится в сборке", 409)
        task = one(c, """
          SELECT t.*,r.expires_at,r.status AS reservation_status,
                 p.sku,p.barcode,p.pick_policy,l.expires_at AS lot_expiry
          FROM pick_tasks t JOIN reservations r ON r.id=t.reservation_id
          JOIN products p ON p.id=r.product_id
          JOIN lots l ON l.id=r.lot_id
          WHERE t.id=%s FOR UPDATE OF t
        """, (task_id,))
        if not task:
            error("NOT_FOUND", "Задание не найдено", 404)
        warehouse_access(user, task["warehouse_id"])
        if task["status"] != "pending":
            error("TASK_STATE", "Задание уже подтверждено", 409)
        if task["assigned_to"] and str(task["assigned_to"]) != str(user["id"]) and user["role"] != "admin":
            error("WRONG_ASSIGNEE", "Задание назначено другому сотруднику", 403)
        if task["reservation_status"] != "active" or task["expires_at"] <= datetime.now(timezone.utc):
            error("EXPIRED_RESERVATION", "Срок резерва истёк", 409)
        if (task["pick_policy"] == "FEFO" and task["lot_expiry"] is not None
                and task["lot_expiry"] < datetime.now(timezone.utc).date()):
            error("EXPIRED_LOT", "Партия просрочена; отбор запрещён", 409)
        if data.barcode not in (task["sku"], task["barcode"]):
            error("BARCODE_MISMATCH", "Отсканирован другой товар", 409)
        if data.actualQty > task["expected_qty"]:
            error("OVERPICK", "Факт превышает резерв", 409)
        state = "completed" if data.actualQty == task["expected_qty"] else "shortage_pending"
        c.execute("""
          UPDATE pick_tasks SET actual_qty=%s,status=%s,counted_by=%s,completed_at=now()
          WHERE id=%s
        """, (data.actualQty, state, user["id"], task_id))
        c.execute("""
          UPDATE orders SET status='picking',updated_at=now()
          WHERE id=%s AND status='confirmed'
        """, (task["order_id"],))
        refresh_order_state(c, task["order_id"])
        audit(c, user["id"], "pick.count", "pick", task_id, after={
            "expected": task["expected_qty"], "actual": data.actualQty, "state": state,
        }, warehouse=task["warehouse_id"])
        return {"id": task_id, "status": state, "actualQty": data.actualQty}


@router.post("/picks/{task_id}/approve-shortage", tags=["Picking"])
def approve_shortage(task_id: str, user=Depends(require("admin", "supervisor"))):
    with pool.connection() as c, c.transaction():
        task_ref = one(c, "SELECT order_id FROM pick_tasks WHERE id=%s", (task_id,))
        if not task_ref:
            error("NOT_FOUND", "Задание не найдено", 404)
        order = one(c, "SELECT * FROM orders WHERE id=%s FOR UPDATE", (task_ref["order_id"],))
        if order["status"] not in ("confirmed", "picking"):
            error("ORDER_STATE", "Заказ не находится в сборке", 409)
        task = one(c, "SELECT * FROM pick_tasks WHERE id=%s FOR UPDATE", (task_id,))
        warehouse_access(user, task["warehouse_id"])
        if task["status"] != "shortage_pending":
            error("TASK_STATE", "Нет ожидающего решения недобора", 409)
        c.execute("UPDATE pick_tasks SET status='accepted',approved_by=%s WHERE id=%s",
                  (user["id"], task_id))
        refresh_order_state(c, task["order_id"])
        audit(c, user["id"], "pick.shortage_approve", "pick", task_id,
              after={"actual": task["actual_qty"], "expected": task["expected_qty"]},
              warehouse=task["warehouse_id"])
        return {"id": task_id, "status": "accepted"}


@router.get("/shipments", tags=["Shipping"])
def shipments(user=Depends(require("admin", "manager", "picker", "supervisor"))):
    with pool.connection() as c:
        return clean(all_rows(c, """
          SELECT s.*,o.customer,o.status AS order_status FROM shipments s
          JOIN orders o ON o.id=s.order_id
          WHERE %s OR s.warehouse_id=ANY(%s::uuid[])
          ORDER BY s.id DESC LIMIT 100
        """, (user["role"] == "admin", user["warehouses"])))


@router.post("/shipments/{shipment_id}/confirm", tags=["Shipping"])
def confirm_shipment(shipment_id: str,
                     idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                     user=Depends(require("admin", "picker", "supervisor"))):
    with pool.connection() as c, c.transaction():
        scope_warehouse = one(c, "SELECT warehouse_id FROM shipments WHERE id=%s",
                              (shipment_id,))
        if not scope_warehouse:
            error("NOT_FOUND", "Отгрузка не найдена", 404)
        warehouse_access(user, scope_warehouse["warehouse_id"])
        digest, previous = replay(c, "shipment:" + shipment_id, idempotency_key, {})
        if previous is not None:
            return previous
        ship = one(c, "SELECT * FROM shipments WHERE id=%s FOR UPDATE", (shipment_id,))
        if not ship:
            error("NOT_FOUND", "Отгрузка не найдена", 404)
        warehouse_access(user, ship["warehouse_id"])
        if ship["status"] != "draft":
            error("ALREADY_POSTED", "Отгрузка уже проведена", 409)
        order = one(c, "SELECT * FROM orders WHERE id=%s FOR UPDATE", (ship["order_id"],))
        if order["status"] != "picked":
            error("PICKING_INCOMPLETE", "Не все задания по заказу закрыты", 409)
        tasks = all_rows(c, """
          SELECT t.*,r.product_id,r.bin_id,r.lot_id,r.qty,r.shipped_qty,
                 r.status AS reservation_status,r.expires_at
          FROM pick_tasks t JOIN reservations r ON r.id=t.reservation_id
          WHERE t.order_id=%s AND t.warehouse_id=%s ORDER BY t.id
        """, (ship["order_id"], ship["warehouse_id"]))
        if not tasks or any(t["status"] not in ("completed", "accepted") for t in tasks):
            error("PICKING_INCOMPLETE", "Сборка не подтверждена", 409)
        lock_stock(c, [(t["product_id"], ship["warehouse_id"]) for t in tasks])
        now = datetime.now(timezone.utc)
        for task in tasks:
            if task["reservation_status"] != "active" or task["expires_at"] <= now:
                error("EXPIRED_RESERVATION", "Резерв недействителен", 409)
            if task["actual_qty"] is None or task["actual_qty"] > task["qty"]:
                error("INVALID_PICK", "Некорректный факт отбора", 409)
        shipped_qty = 0
        for task in tasks:
            actual = task["actual_qty"]
            movement(c, task["product_id"], ship["warehouse_id"], task["bin_id"],
                     task["lot_id"], -actual, "shipment", shipment_id, user["id"])
            shipped_qty += actual
            c.execute("""
              UPDATE reservations SET shipped_qty=%s,status=%s WHERE id=%s
            """, (actual, "fulfilled" if actual == task["qty"] else "released",
                  task["reservation_id"]))
        c.execute("""
          UPDATE shipments SET status='posted',confirmed_by=%s,posted_at=now()
          WHERE id=%s
        """, (user["id"], shipment_id))
        more = one(c, """
          SELECT 1 FROM shipments WHERE order_id=%s AND id<>%s AND status<>'posted'
        """, (ship["order_id"], shipment_id))
        if not more:
            c.execute("""
              UPDATE orders SET status='shipped',version=version+1,updated_at=now()
              WHERE id=%s
            """, (ship["order_id"],))
        result = {"id": shipment_id, "status": "posted", "shippedQty": shipped_qty,
                  "orderId": str(ship["order_id"])}
        audit(c, user["id"], "shipment.post", "shipment", shipment_id,
              after=result, warehouse=ship["warehouse_id"])
        outbox(c, "shipment.posted", shipment_id, result)
        save_replay(c, "shipment:" + shipment_id, idempotency_key, digest, result)
        return result


@router.get("/shipments/{shipment_id}/returnable", tags=["Shipping"])
def returnable_items(shipment_id: str,
                     user=Depends(require("admin", "manager", "picker", "supervisor"))):
    """Only unreturned actual shipment quantities; never expose another warehouse."""
    with pool.connection() as c:
        shipment = one(c, """
          SELECT id,order_id,warehouse_id,status FROM shipments WHERE id=%s
        """, (shipment_id,))
        if not shipment:
            error("NOT_FOUND", "Отгрузка не найдена", 404)
        warehouse_access(user, shipment["warehouse_id"])
        if shipment["status"] != "posted":
            error("SHIPMENT_STATE", "Отгрузка ещё не проведена", 409)
        rows = all_rows(c, """
          SELECT r.id AS reservation_id,p.sku,p.name,
                 r.shipped_qty,
                 COALESCE(sum(ret.qty),0)::int AS returned_qty,
                 (r.shipped_qty-COALESCE(sum(ret.qty),0))::int AS returnable_qty
          FROM reservations r
          JOIN order_lines l ON l.id=r.order_line_id
          JOIN products p ON p.id=r.product_id
          LEFT JOIN return_lines ret ON ret.reservation_id=r.id
          WHERE l.order_id=%s AND r.warehouse_id=%s
          GROUP BY r.id,p.id
          HAVING r.shipped_qty > COALESCE(sum(ret.qty),0)
          ORDER BY p.sku,r.id
        """, (shipment["order_id"], shipment["warehouse_id"]))
        return {"shipmentId": shipment_id, "warehouseId": str(shipment["warehouse_id"]),
                "items": clean(rows)}


@router.post("/shipments/{shipment_id}/returns", status_code=201, tags=["Shipping"])
def create_return(shipment_id: str, data: ReturnInput,
                  idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
                  user=Depends(require("admin", "picker", "supervisor"))):
    with pool.connection() as c, c.transaction():
        scope_warehouse = one(c, "SELECT warehouse_id FROM shipments WHERE id=%s",
                              (shipment_id,))
        if not scope_warehouse:
            error("NOT_FOUND", "Отгрузка не найдена", 404)
        warehouse_access(user, scope_warehouse["warehouse_id"])
        digest, previous = replay(c, "return:" + shipment_id,
                                  idempotency_key, data.model_dump())
        if previous is not None:
            return previous
        shipment = one(c, "SELECT * FROM shipments WHERE id=%s FOR UPDATE", (shipment_id,))
        if not shipment:
            error("NOT_FOUND", "Отгрузка не найдена", 404)
        warehouse_access(user, shipment["warehouse_id"])
        if shipment["status"] != "posted":
            error("SHIPMENT_STATE", "Возврат возможен только по отгрузке", 409)
        bin_row = check_bin(c, data.binId, user)
        if str(bin_row["warehouse_id"]) != str(shipment["warehouse_id"]):
            error("WRONG_WAREHOUSE", "Возврат должен поступить на склад отгрузки", 409)
        res = one(c, """
          SELECT r.* FROM reservations r
          JOIN order_lines l ON l.id=r.order_line_id
          WHERE r.id=%s AND l.order_id=%s AND r.warehouse_id=%s
        """, (data.reservationId, shipment["order_id"], shipment["warehouse_id"]))
        if not res:
            error("RETURN_REFERENCE", "Позиция отсутствует в исходной отгрузке", 404)
        lock_stock(c, [(res["product_id"], res["warehouse_id"])])
        returned = one(c, """
          SELECT COALESCE(sum(qty),0)::int AS qty FROM return_lines WHERE reservation_id=%s
        """, (data.reservationId,))["qty"]
        if returned + data.qty > res["shipped_qty"]:
            error("RETURN_EXCEEDS_SHIPPED", "Возврат больше невозвращённого количества", 409,
                  {"shipped": res["shipped_qty"], "returned": returned})
        record = one(c, """
          INSERT INTO return_lines(shipment_id,reservation_id,bin_id,qty,actor_id)
          VALUES(%s,%s,%s,%s,%s) RETURNING id
        """, (shipment_id, data.reservationId, data.binId, data.qty, user["id"]))
        movement(c, res["product_id"], res["warehouse_id"], data.binId,
                 res["lot_id"], data.qty, "return", record["id"], user["id"])
        result = {"id": str(record["id"]), "qty": data.qty, "shipmentId": shipment_id}
        audit(c, user["id"], "return.post", "return", record["id"],
              after=result, warehouse=res["warehouse_id"])
        save_replay(c, "return:" + shipment_id,
                    idempotency_key, digest, result, 201)
        return result


@router.post("/blocks", status_code=201, tags=["Stocks"])
def block_stock(data: BlockInput, user=Depends(require("admin", "supervisor"))):
    with pool.connection() as c, c.transaction():
        bin_row = check_bin(c, data.binId, user)
        check_lot(c, data.lotId, data.productId)
        lock_stock(c, [(data.productId, bin_row["warehouse_id"])])
        available = location_available(c, data.productId, bin_row["warehouse_id"],
                                       data.binId, data.lotId)
        if available < data.qty:
            error("INSUFFICIENT_STOCK", "Недостаточно свободного товара", 409,
                  {"available": available})
        record = one(c, """
          INSERT INTO stock_blocks(product_id,warehouse_id,bin_id,lot_id,qty,reason,actor_id)
          VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING *
        """, (data.productId, bin_row["warehouse_id"], data.binId, data.lotId,
              data.qty, data.reason, user["id"]))
        audit(c, user["id"], "stock.block", "stock_block", record["id"],
              after={"qty": data.qty, "reason": data.reason},
              warehouse=bin_row["warehouse_id"])
        return clean(record)


@router.post("/blocks/{block_id}/release", tags=["Stocks"])
def release_block(block_id: str, user=Depends(require("admin", "supervisor"))):
    with pool.connection() as c, c.transaction():
        block = one(c, "SELECT * FROM stock_blocks WHERE id=%s", (block_id,))
        if not block:
            error("NOT_FOUND", "Блокировка не найдена", 404)
        warehouse_access(user, block["warehouse_id"])
        lock_stock(c, [(block["product_id"], block["warehouse_id"])])
        changed = c.execute("""
          UPDATE stock_blocks SET active=false WHERE id=%s AND active RETURNING id
        """, (block_id,)).fetchone()
        if not changed:
            error("ALREADY_RELEASED", "Блокировка уже снята", 409)
        audit(c, user["id"], "stock.unblock", "stock_block", block_id,
              before={"active": True}, after={"active": False},
              warehouse=block["warehouse_id"])
        return {"id": block_id, "active": False}
