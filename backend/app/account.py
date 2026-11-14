"""Account self-service operations. Tokens are revoked by incrementing version."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from .auth import current_user, hash_password, verify_password
from .db import audit, error, one, pool

router = APIRouter()


class ChangePassword(BaseModel):
    currentPassword: str = Field(min_length=1)
    newPassword: str = Field(min_length=12, max_length=128)


@router.post("/auth/change-password", tags=["Auth"])
def change_password(data: ChangePassword, user=Depends(current_user)):
    if data.newPassword == data.currentPassword:
        error("PASSWORD_REUSE", "Новый пароль должен отличаться от прежнего", 400)
    with pool.connection() as c, c.transaction():
        row = one(c, "SELECT password_hash FROM users WHERE id=%s FOR UPDATE",
                  (user["id"],))
        if not row or not verify_password(data.currentPassword, row["password_hash"]):
            error("BAD_PASSWORD", "Текущий пароль указан неверно", 403)
        c.execute("""
          UPDATE users SET password_hash=%s,token_version=token_version+1
          WHERE id=%s
        """, (hash_password(data.newPassword), user["id"]))
        audit(c, user["id"], "auth.password_change", "user", user["id"],
              after={"sessionsRevoked": True})
    return {"passwordChanged": True, "sessionsRevoked": True}


@router.post("/auth/logout-all", tags=["Auth"])
def logout_all(user=Depends(current_user)):
    with pool.connection() as c, c.transaction():
        c.execute("UPDATE users SET token_version=token_version+1 WHERE id=%s",
                  (user["id"],))
        audit(c, user["id"], "auth.logout_all", "user", user["id"],
              after={"sessionsRevoked": True})
    return {"sessionsRevoked": True}
