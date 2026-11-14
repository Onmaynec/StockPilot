"""One-time first administrator bootstrap for a production deployment."""
import os

from .auth import hash_password
from .catalog import normalize_email
from .config import validate_runtime_config
from .db import audit, one, pool
from .security_check import inspect_runtime_role


def main():
    config = validate_runtime_config()
    if config["environment"] != "production":
        raise RuntimeError("The bootstrap command is available only in production mode.")

    email = normalize_email(os.getenv("STOCKPILOT_BOOTSTRAP_ADMIN_EMAIL", ""))
    password = os.getenv("STOCKPILOT_BOOTSTRAP_ADMIN_PASSWORD", "")
    full_name = os.getenv("STOCKPILOT_BOOTSTRAP_ADMIN_NAME", "").strip()
    if len(password) < 16 or len(password) > 128:
        raise RuntimeError("Bootstrap password must contain 16..128 characters.")
    if not full_name or len(full_name) > 120:
        raise RuntimeError("STOCKPILOT_BOOTSTRAP_ADMIN_NAME must contain 1..120 characters.")

    pool.open(wait=True)
    try:
        with pool.connection() as c, c.transaction():
            inspect_runtime_role(c)
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s))",
                ("stockpilot.bootstrap-admin",),
            )
            existing = one(c, "SELECT id FROM users WHERE role='admin' LIMIT 1")
            if existing:
                raise RuntimeError(
                    "An administrator already exists; bootstrap is strictly one-time."
                )
            admin = one(c, """
              INSERT INTO users(email,password_hash,role,full_name)
              VALUES(%s,%s,'admin',%s) RETURNING id,email
            """, (email, hash_password(password), full_name))
            audit(c, None, "admin.bootstrap", "user", admin["id"],
                  after={"email": admin["email"], "bootstrap": True})
        print("Initial administrator created: " + admin["email"])
        print("Remove the bootstrap environment variables from the shell after this command.")
    finally:
        pool.close()


if __name__ == "__main__":
    main()
