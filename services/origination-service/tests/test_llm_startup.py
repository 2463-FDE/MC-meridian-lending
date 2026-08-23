"""LLM feature startup wiring (review comment 2).

load_llm_config() must run at application startup so a deploy missing
CLAUDE_API_KEY fails loud at boot instead of on the first customer summary. The
feature is opt-in via LLM_ENABLED; when off, startup requires no LLM env (so
import/health smoke and non-summary deployments start clean).

TestClient used as a context manager runs the app lifespan, so entering the
context is what triggers — or fails — startup validation.
"""

import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import config
from app.llm import ClaudeClient, LLMConfigError
from app.main import app, get_llm_client
from tests.test_db_readiness import READABLE_DOB_CONSTRAINT_DEF


class _Req:
    """Minimal stand-in for fastapi.Request — get_llm_client only reads .app."""

    def __init__(self, app):
        self.app = app


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql="", *a, **k):
        self._last = sql

    def fetchone(self):
        # The ck_applicants_dob_readable rung compares the constraint DEFINITION, not just
        # the name, so a ready-DB stub has to answer with it -- reuse the one constant
        # test_db_readiness pins against config._DOB_READABLE_EXPECTED_DEF rather than
        # copying the literal here, where it would drift unnoticed.
        if "pg_get_constraintdef" in getattr(self, "_last", ""):
            return (READABLE_DOB_CONSTRAINT_DEF,)
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
    # /health now also requires the internal-service token (PR review); set it so this
    # asserts the LLM-off path, not the token gate.
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "tok")
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


# --- the gate has to be reachable from where operators actually set it -------------
#
# LLM_ENABLED decides whether any LLM route serves at all, and every other variable the
# feature needs -- CLAUDE_API_KEY, CLAUDE_PROVIDER, AWS_*, LANGSMITH_* -- is interpolated
# from the host environment in docker-compose.yml, because the documented rule is that
# credentials live in the shell and never in the committed .env. The gate itself was not:
# it reached the container only through `env_file: .env`, so the documented workflow could
# supply the credential and still leave the feature off, with no error to explain why. The
# only way in was hand-editing .env, which is not a reproducible demo step.
#
# The stanza reader is deliberately narrow rather than a copy of test_rag_eval_seam.py's
# fuller `_compose_service_block`: this asserts one line's shape, and a second copy of that
# helper is the kind of duplication `redactor-drift` exists to police.

# tests/ -> origination-service/ -> services/ -> repo root
REPO = Path(__file__).resolve().parents[3]


def _origination_environment_block() -> str:
    """origination-service's `environment:` mapping in the BASE compose file.

    Asserts the stanza exists: a check that silently finds nothing must not report success
    over a file it never read.
    """
    lines = (REPO / "docker-compose.yml").read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line == "  origination-service:"]
    assert starts, "no `  origination-service:` stanza in docker-compose.yml"
    block = []
    for line in lines[starts[0] + 1 :]:
        if re.match(r"^  \S", line):  # next service key, same indent
            break
        block.append(line)
    assert block, "origination-service stanza in docker-compose.yml is empty"
    return "\n".join(block)


def test_compose_lets_the_host_env_set_the_llm_feature_gate():
    block = _origination_environment_block()
    assert re.search(r"^\s+LLM_ENABLED:\s*\$\{LLM_ENABLED:-.*\}\s*$", block, re.M), (
        "docker-compose.yml must interpolate LLM_ENABLED for origination-service, or "
        "`export LLM_ENABLED=true` before `compose up` is silently ignored and the only "
        "way to enable the feature is editing the committed .env"
    )


def test_compose_leaves_the_llm_feature_gate_off_by_default():
    """The interpolation default must be empty, so absence of the variable means off.

    A default of "true" here would enable the feature for every `compose up`, and an
    enabled feature with no credential aborts origination's startup (load_llm_config
    raises in lifespan) rather than degrading -- so a wrong default breaks the whole
    stack, not just the LLM routes.
    """
    block = _origination_environment_block()
    match = re.search(
        r"^\s+LLM_ENABLED:\s*\$\{LLM_ENABLED:-(?P<default>.*)\}\s*$", block, re.M
    )
    assert match, "LLM_ENABLED is not interpolated with a default"
    default = match.group("default").strip().strip('"').strip("'")
    assert default == "", f"LLM_ENABLED must default to empty (off), not {default!r}"
