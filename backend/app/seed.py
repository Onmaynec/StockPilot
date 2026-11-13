"""Repeatable demo fixture: 3 warehouses, 12k products, 200 customers, 30 users."""
import os
from datetime import datetime, timedelta, timezone
from random import Random
from uuid import NAMESPACE_DNS, uuid5

from .auth import hash_password
from .db import pool, one

rng = Random(42)


def uid(name):
    return str(uuid5(NAMESPACE_DNS, "stockpilot:" + name))


def seed():
    password = os.getenv("DEMO_PASSWORD")
    if not password or len(password) < 12:
        raise RuntimeError("DEMO_PASSWORD (12+ characters) is required for demo seeding.")
    pool.open(wait=True)
    try:
        with pool.connection() as c, c.transaction():
            if one(c, "SELECT 1 FROM stock_movements WHERE doc_type='seed' LIMIT 1"):
                print("Demo inventory already exists; nothing changed.")
                return
            warehouses = []
            bins = []
            for index in range(1, 4):
                w = uid(f"warehouse:{index}")
                z = uid(f"zone:{index}")
                b = uid(f"bin:{index}")
                warehouses.append(w)
                bins.append(b)
                c.execute("""
                  INSERT INTO warehouses(id,code,name) VALUES(%s,%s,%s)
                  ON CONFLICT(id) DO NOTHING
                """, (w, f"W{index}", f"Склад №{index}"))
                c.execute("""
                  INSERT INTO zones(id,warehouse_id,code,name) VALUES(%s,%s,%s,%s)
                  ON CONFLICT(id) DO NOTHING
                """, (z, w, "A", "Основная зона"))
                c.execute("""
                  INSERT INTO bins(id,warehouse_id,zone_id,code) VALUES(%s,%s,%s,%s)
                  ON CONFLICT(id) DO NOTHING
                """, (b, w, z, "A-01"))
            c.cursor().executemany("""
              INSERT INTO customers(id,name) VALUES(%s,%s)
              ON CONFLICT(id) DO NOTHING
            """, [(uid(f"customer:{i}"), f"Клиент {i:03d}") for i in range(1, 201)])
            roles = (["admin"] + ["manager"] * 10 + ["picker"] * 15
                     + ["supervisor"] * 3 + ["director"])
            role_names = {"admin": "Администратор", "manager": "Менеджер",
                          "picker": "Кладовщик", "supervisor": "Старший смены",
                          "director": "Руководитель"}
            hashed = hash_password(password)
            users = []
            for i, role in enumerate(roles, 1):
                user_id = uid(f"user:{i}")
                email = f"{role}{i}@stockpilot.local"
                if i == 1:
                    email = "admin@stockpilot.local"
                if role == "manager" and i == 2:
                    email = "manager@stockpilot.local"
                if role == "picker" and i == 12:
                    email = "picker@stockpilot.local"
                if role == "supervisor" and i == 27:
                    email = "supervisor@stockpilot.local"
                if role == "director":
                    email = "director@stockpilot.local"
                users.append((user_id, email, hashed, role, f"{role_names[role]} {i}"))
            c.cursor().executemany("""
              INSERT INTO users(id,email,password_hash,role,full_name)
              VALUES (%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING
            """, users)
            c.cursor().executemany("""
              INSERT INTO user_warehouses(user_id,warehouse_id)
              VALUES(%s,%s) ON CONFLICT DO NOTHING
            """, [(u[0], w) for u in users for w in warehouses])
            products = []
            lots = []
            movements = []
            guards = []
            base = datetime.now(timezone.utc)
            for i in range(1, 12001):
                product = uid(f"product:{i}")
                lot = uid(f"lot:{i}")
                wh_index = (i - 1) % 3
                policy = "FEFO" if i % 7 == 0 else "FIFO"
                expires = (base.date() + timedelta(days=-1 if i % 101 == 0 else 365))
                products.append((product, f"SKU-{i:05d}", f"Комплектующая {i:05d}",
                                 "шт", f"460{i:010d}", "Комплектующие", policy))
                lots.append((lot, product, f"LOT-{i:05d}", expires))
                qty = rng.randint(8, 100)
                posted = base - timedelta(days=rng.randint(0, 89), hours=rng.randint(0, 23))
                guards.append((product, warehouses[wh_index]))
                movements.append((product, warehouses[wh_index], bins[wh_index],
                                  lot, qty, "seed", uid(f"seed:{i}"), users[0][0], posted))
            # Bulk inserts avoid thousands of network round trips.
            for offset in range(0, len(products), 1000):
                c.cursor().executemany("""
                  INSERT INTO products(id,sku,name,unit,barcode,category,pick_policy)
                  VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING
                """, products[offset:offset + 1000])
            for offset in range(0, len(lots), 1000):
                c.cursor().executemany("""
                  INSERT INTO lots(id,product_id,code,expires_at)
                  VALUES(%s,%s,%s,%s) ON CONFLICT(id) DO NOTHING
                """, lots[offset:offset + 1000])
            c.cursor().executemany("""
              INSERT INTO stock_guards(product_id,warehouse_id)
              VALUES(%s,%s) ON CONFLICT DO NOTHING
            """, guards)
            for offset in range(0, len(movements), 1000):
                c.cursor().executemany("""
                  INSERT INTO stock_movements
                  (product_id,warehouse_id,bin_id,lot_id,qty,doc_type,doc_id,actor_id,created_at)
                  VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)
                """, movements[offset:offset + 1000])
            print("Seed complete: 3 warehouses, 12,000 SKU, 200 customers, 30 users, 90 days.")
            print("Credentials: admin@stockpilot.local / DEMO_PASSWORD (from .env)")
    finally:
        pool.close()


if __name__ == "__main__":
    seed()
