"""Fail closed when the API/worker database principal is over-privileged."""
import json
import os

from .db import one, pool


def inspect_runtime_role(c):
    record = one(c, """
      SELECT current_user AS role_name, rol.rolsuper, rol.rolcreaterole,
             rol.rolcreatedb, rol.rolreplication, rol.rolbypassrls, rol.rolinherit,
             has_schema_privilege(current_user,'public','CREATE') AS can_create_schema,
             EXISTS (
               SELECT 1 FROM pg_class obj
               JOIN pg_namespace ns ON ns.oid=obj.relnamespace
               WHERE ns.nspname='public' AND obj.relowner=rol.oid
             ) AS owns_public_objects,
             has_table_privilege(current_user,'public.stock_movements','SELECT') AS ledger_read,
             has_table_privilege(current_user,'public.stock_movements','INSERT') AS ledger_insert,
             has_table_privilege(current_user,'public.stock_movements','UPDATE') AS ledger_update,
             has_table_privilege(current_user,'public.stock_movements','DELETE') AS ledger_delete,
             has_table_privilege(current_user,'public.audit_events','UPDATE') AS audit_update,
             has_table_privilege(current_user,'public.audit_events','DELETE') AS audit_delete,
             has_table_privilege(current_user,'public.users','DELETE') AS can_delete_users,
             has_table_privilege(current_user,'public.products','DELETE') AS can_delete_products,
             has_table_privilege(current_user,'public.orders','DELETE') AS can_delete_orders,
             has_table_privilege(current_user,'public.user_warehouses','DELETE') AS can_delete_user_warehouses,
             has_table_privilege(current_user,'public.rate_limit_counters','DELETE') AS can_delete_rate_limits
      FROM pg_roles rol WHERE rol.rolname=current_user
    """)
    if not record:
        raise RuntimeError("Current PostgreSQL role was not found")
    denied = (
        "rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication",
        "rolbypassrls", "rolinherit", "can_create_schema", "owns_public_objects",
        "ledger_update", "ledger_delete", "audit_update", "audit_delete",
        "can_delete_users", "can_delete_products", "can_delete_orders",
    )
    accepted = all(not record[key] for key in denied)
    accepted = accepted and record["ledger_read"] and record["ledger_insert"]
    accepted = accepted and record["can_delete_user_warehouses"]
    accepted = accepted and record["can_delete_rate_limits"]
    expected = os.getenv("APP_DB_USER")
    if expected and record["role_name"] != expected:
        accepted = False
    if record["role_name"] == "postgres":
        accepted = False
    if not accepted:
        raise RuntimeError(
            "Application database role has excessive privileges or misses required DML grants"
        )
    return record


def main():
    pool.open(wait=True)
    try:
        with pool.connection() as c:
            record = inspect_runtime_role(c)
            print(json.dumps({
                "appRole": record["role_name"],
                "checksPassed": True,
                "canAlterSchema": record["can_create_schema"],
                "ownsPublicObjects": record["owns_public_objects"],
                "canRewriteLedger": record["ledger_update"] or record["ledger_delete"],
                "canDeleteBusinessRows": (
                    record["can_delete_users"] or record["can_delete_products"]
                    or record["can_delete_orders"]
                ),
            }, ensure_ascii=False))
    finally:
        pool.close()


if __name__ == "__main__":
    main()
