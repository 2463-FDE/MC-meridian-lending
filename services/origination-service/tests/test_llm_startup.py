"""LLM feature startup wiring (review comment 2).

load_llm_config() must run at application startup so a deploy missing
CLAUDE_API_KEY fails loud at boot instead of on the first customer summary. The
feature is opt-in via LLM_ENABLED; when off, startup requires no LLM env (so
import/health smoke and non-summary deployments start clean).

TestClient used as a context manager runs the app lifespan, so entering the
context is what triggers — or fails — startup validation.
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import config
from app.llm import ClaudeClient, LLMConfigError
from app.main import app, get_llm_client


class _Req:
    """Minimal stand-in for fastapi.Request — get_llm_client only reads .app."""

    def __init__(self, app):
        self.app = app


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *a, **k):
        pass

    def fetchone(self):
        return (1,)


class _FakeConn:
    """Stubs the DB-readiness probe so /health passes without a real Postgres."""

    def cursor(self):
        return _FakeCursor()

    def close(self):
        pass


def test_startup_skips_llm_when_disabled(monkeypatch):
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)  # not required when off
    # /health now gates on DB readiness (DB-readiness security gate merged from
    # main); stub a reachable DB so this asserts the LLM-off path, not DB config.
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(config.psycopg2, "connect", lambda *a, **k: _FakeConn())
    config.reset_database_probe_cache()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert app.state.llm_client is None
        assert app.state.llm_config is None
    config.reset_database_probe_cache()


def test_startup_fails_loud_when_enabled_without_key(monkeypatch):
    # provider=anthropic + LLM enabled + no key => startup must raise, aborting boot.
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_PROVIDER", "anthropic")
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    with pytest.raises(LLMConfigError):
        with TestClient(app):
            pass  # entering the context runs lifespan startup


def test_startup_initializes_client_when_enabled_with_key(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "1")
    monkeypatch.setenv("CLAUDE_PROVIDER", "anthropic")
    monkeypatch.setenv("CLAUDE_API_KEY", "test-key")
    with TestClient(app):
        assert isinstance(app.state.llm_client, ClaudeClient)
        assert app.state.llm_config is not None
        assert app.state.llm_config.provider == "anthropic"


def test_get_llm_client_returns_client_when_enabled(monkeypatch):
    monkeypatch.setenv("LLM_ENABLED", "1")
    monkeypatch.setenv("CLAUDE_API_KEY", "test-key")
    with TestClient(app):
        assert isinstance(get_llm_client(_Req(app)), ClaudeClient)


def test_get_llm_client_503_when_disabled(monkeypatch):
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    with TestClient(app):
        with pytest.raises(HTTPException) as exc_info:
            get_llm_client(_Req(app))
        assert exc_info.value.status_code == 503
