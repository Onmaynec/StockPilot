import secrets

import pytest

from app.config import validate_runtime_config


def production_env(monkeypatch):
    values = {
        "STOCKPILOT_ENV": "production",
        "JWT_SECRET": secrets.token_urlsafe(48),
        "CORS_ORIGINS": "https://wms.example.com",
        "DEMO_PASSWORD": "",
        "TRUSTED_PROXY_CIDRS": "172.30.0.2/32",
        "APP_DB_USER": "stockpilot_app",
        "POSTGRES_USER": "stockpilot",
        "DATABASE_URL": "postgresql://stockpilot_app:db-secret@db:5432/stockpilot",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)


def test_production_configuration_accepts_separate_runtime_role(monkeypatch):
    production_env(monkeypatch)
    result = validate_runtime_config()
    assert result["environment"] == "production"
    assert result["databaseRole"] == "stockpilot_app"
    assert result["trustedProxyCount"] == 1


@pytest.mark.parametrize("field,value", [
    ("JWT_SECRET", "replace_with_a_random_secret_" + "x" * 40),
    ("CORS_ORIGINS", "http://wms.example.com"),
    ("DEMO_PASSWORD", "DemoStock2026!"),
    ("TRUSTED_PROXY_CIDRS", "172.30.0.0/24"),
    ("DATABASE_URL", "postgresql://stockpilot:owner-secret@db:5432/stockpilot"),
])
def test_production_configuration_rejects_unsafe_defaults(monkeypatch, field, value):
    production_env(monkeypatch)
    monkeypatch.setenv(field, value)
    with pytest.raises(RuntimeError):
        validate_runtime_config()


def test_development_configuration_keeps_local_http_origins(monkeypatch):
    monkeypatch.setenv("STOCKPILOT_ENV", "development")
    monkeypatch.setenv("JWT_SECRET", "development-secret-that-is-at-least-32-chars")
    monkeypatch.setenv("CORS_ORIGINS", "http://localhost:8080")
    assert validate_runtime_config()["environment"] == "development"
