"""Fail-fast validation for production runtime configuration."""
import ipaddress
import os
from urllib.parse import unquote, urlsplit


def validate_runtime_config():
    mode = os.getenv("STOCKPILOT_ENV", "development").strip().lower()
    if mode not in {"development", "test", "production"}:
        raise RuntimeError("STOCKPILOT_ENV must be development, test or production")

    secret = os.getenv("JWT_SECRET", "")
    if len(secret) < 32:
        raise RuntimeError("JWT_SECRET must contain at least 32 characters")
    if mode != "production":
        return {"environment": mode}

    lowered = secret.lower()
    placeholders = ("replace_with", "change_me", "local_", "ci_only", "demo", "password")
    if len(secret) < 48 or any(marker in lowered for marker in placeholders):
        raise RuntimeError(
            "Production JWT_SECRET must be a generated secret of at least 48 characters"
        )

    origins = [value.strip() for value in os.getenv("CORS_ORIGINS", "").split(",")
               if value.strip()]
    if not origins:
        raise RuntimeError("Production CORS_ORIGINS must contain at least one HTTPS origin")
    for origin in origins:
        parsed = urlsplit(origin)
        if (parsed.scheme != "https" or not parsed.netloc or parsed.username
                or parsed.password or parsed.path not in ("", "/")
                or parsed.query or parsed.fragment):
            raise RuntimeError("Every production CORS_ORIGINS entry must be an HTTPS origin")

    if os.getenv("DEMO_PASSWORD", "").strip():
        raise RuntimeError("DEMO_PASSWORD must be empty in production")

    trusted = [value.strip() for value in os.getenv("TRUSTED_PROXY_CIDRS", "").split(",")
               if value.strip()]
    if not trusted:
        raise RuntimeError("Production TRUSTED_PROXY_CIDRS must list the trusted proxy address")
    for value in trusted:
        try:
            network = ipaddress.ip_network(value, strict=False)
        except ValueError as exc:
            raise RuntimeError("Invalid address in TRUSTED_PROXY_CIDRS") from exc
        if network.prefixlen != network.max_prefixlen:
            raise RuntimeError(
                "Production TRUSTED_PROXY_CIDRS must contain individual proxy addresses (/32 or /128)"
            )

    database = urlsplit(os.getenv("DATABASE_URL", ""))
    database_user = unquote(database.username or "")
    app_user = os.getenv("APP_DB_USER", "")
    owner_user = os.getenv("POSTGRES_USER", "")
    if not app_user or not owner_user or app_user == owner_user or database_user != app_user:
        raise RuntimeError(
            "Production DATABASE_URL must use APP_DB_USER, separate from POSTGRES_USER"
        )

    return {"environment": mode, "trustedProxyCount": len(trusted),
            "corsOriginCount": len(origins), "databaseRole": app_user}
