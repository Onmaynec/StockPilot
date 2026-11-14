"""Authentication and server-side role/warehouse authorization."""
import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .db import error, one, pool

bearer = HTTPBearer(auto_error=False)
ROLES = {"admin", "manager", "picker", "supervisor", "director"}


def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 320_000)
    return "pbkdf2_sha256$320000$" + salt.hex() + "$" + digest.hex()


def verify_password(password, stored):
    try:
        kind, count, salt, expected = stored.split("$")
        if kind != "pbkdf2_sha256":
            return False
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt), int(count)
        )
        return hmac.compare_digest(actual, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def issue_token(user):
    secret = os.environ["JWT_SECRET"]
    if len(secret) < 32:
        raise RuntimeError("JWT_SECRET must have at least 32 characters")
    now = datetime.now(timezone.utc)
    return jwt.encode({
        "sub": str(user["id"]), "ver": user["token_version"],
        "iat": now, "exp": now + timedelta(hours=8),
    }, secret, algorithm="HS256")


def current_user(credentials: HTTPAuthorizationCredentials | None = Depends(bearer)):
    if credentials is None:
        raise HTTPException(status_code=401, detail={
            "code": "UNAUTHORIZED", "message": "Требуется вход", "details": None,
        })
    try:
        payload = jwt.decode(
            credentials.credentials, os.environ["JWT_SECRET"], algorithms=["HS256"]
        )
        uid = payload["sub"]
    except (jwt.PyJWTError, KeyError):
        error("UNAUTHORIZED", "Сессия недействительна", 401)
    with pool.connection() as c:
        user = one(c, "SELECT id,email,role,full_name,active,token_version FROM users WHERE id=%s", (uid,))
        if not user or not user["active"] or payload.get("ver") != user["token_version"]:
            error("UNAUTHORIZED", "Сессия отозвана или пользователь неактивен", 401)
        user["warehouses"] = [
            str(row["warehouse_id"]) for row in c.execute(
                "SELECT warehouse_id FROM user_warehouses WHERE user_id=%s", (uid,)
            ).fetchall()
        ]
        return user


def require(*roles):
    def dependency(user=Depends(current_user)):
        if user["role"] not in roles:
            error("FORBIDDEN", "Недостаточно прав для операции", 403)
        return user
    return dependency


def warehouse_access(user, warehouse_id):
    if user["role"] == "admin":
        return
    if str(warehouse_id) not in user["warehouses"]:
        error("WAREHOUSE_FORBIDDEN", "Нет доступа к выбранному складу", 403)
