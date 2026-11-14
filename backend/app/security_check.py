"""Exit non-zero if the runtime database connection has elevated privileges."""
import json

from .db import one, pool


def main():
    pool.open(wait=True)
    try:
        with pool.connection() as c:
            record = one(c, """
              SELECT current_user AS role_name, rol.rolsuper, rol.rolcreaterole,
                     rol.rolcreatedb, rol.rolreplication,
                     has_schema_privilege(current_user,'public','CREATE') AS can_create_schema,
                     has_table_privilege(current_user,'public.stock_movements','SELECT') AS ledger_read,
                     has_table_privilege(current_user,'public.stock_movements','INSERT') AS ledger_insert,
                     has_table_privilege(current_user,'public.stock_movements','UPDATE') AS ledger_update,
                     has_table_privilege(current_user,'public.stock_movements','DELETE') AS ledger_delete,
                     has_table_privilege(current_user,'public.audit_events','UPDATE') AS audit_update,
                     has_table_privilege(current_user,'public.audit_events','DELETE') AS audit_delete
              FROM pg_roles rol WHERE rol.rolname=current_user
            """)
            denied = ["rolsuper", "rolcreaterole", "rolcreatedb", "rolreplication",
                      "can_create_schema", "ledger_update", "ledger_delete",
                      "audit_update", "audit_delete"]
            accepted = all(not record[key] for key in denied)
            accepted = accepted and record["ledger_read"] and record["ledger_insert"]
            if record["role_name"] in ("postgres",):
                accepted = False
            print(json.dumps({"appRole": record["role_name"],
                              "checksPassed": bool(accepted),
                              "canAlterSchema": record["can_create_schema"],
                              "canRewriteLedger": record["ledger_update"] or record["ledger_delete"]},
                             ensure_ascii=False))
            if not accepted:
                raise SystemExit("Application database role must not have DDL or ledger update/delete rights.")
    finally:
        pool.close()


if __name__ == "__main__":
    main()
