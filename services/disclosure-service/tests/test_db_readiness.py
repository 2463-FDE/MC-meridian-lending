"""DB readiness regression.

An unset or passwordless DATABASE_URL must read as misconfigured so /health can
report unhealthy instead of connecting unauthenticated. Covers the passwordless
DSN (meridian:@postgres) the secret purge left behind across every service.
"""
from app import config


def test_unset_database_url_is_misconfigured(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_passwordless_database_url_is_misconfigured(monkeypatch):
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:@postgres:5432/meridian"
    )
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_password_bearing_database_url_is_ok(monkeypatch):
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:secret@postgres:5432/meridian"
    )
    assert config.database_url_configured() is True
    assert "DATABASE_URL" not in config.missing_required_secrets()
