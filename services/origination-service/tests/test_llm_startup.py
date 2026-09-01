"""LLM feature startup wiring (review comment 2).

load_llm_config() must run at application startup so a deploy missing
CLAUDE_API_KEY fails loud at boot instead of on the first customer summary. The
feature is opt-in via LLM_ENABLED; when off, startup requires no LLM env (so
import/health smoke and non-summary deployments start clean).

TestClient used as a context manager runs the app lifespan, so entering the
context is what triggers — or fails — startup validation.
"""

import logging
import re
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import config
from app.llm import ClaudeClient, LLMConfigError
from app.main import app, get_llm_client
from tests.test_db_readiness import (
    ASSISTANT_RUNS_CHECK_DEFS,
    ASSISTANT_RUNS_COLUMN_ROWS,
    READABLE_DOB_CONSTRAINT_DEF,
)


class _Url:
    def __init__(self, path):
        self.path = path


class _Req:
    """Minimal stand-in for fastapi.Request — get_llm_client reads .app, and on the
    disabled path .method/.url.path for its refusal log line (audit item 8)."""

    def __init__(self, app, method="GET", path="/assistant/decisions/1"):
        self.app = app
        self.method = method
        self.url = _Url(path)


class _FakeCursor:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql="", *a, **k):
        self._last = sql

    def fetchone(self):
        # The ck_applicants_dob_readable rung compares the constraint DEFINITION, not just
        # the name, so a ready-DB stub has to answer with it -- reuse the constant
        # test_db_readiness pins against config._DOB_READABLE_EXPECTED_DEF rather than
        # copying the literal here, where it would drift unnoticed. Same for the three
        # assistant_runs CHECKs, which the shape rung now compares in full.
        last = getattr(self, "_last", "")
        if last.strip() == "SELECT to_regclass('assistant_runs')":
            return ("assistant_runs",)
        if "pg_get_constraintdef" in last:
            for conname, definition in ASSISTANT_RUNS_CHECK_DEFS.items():
                if "conname = '" + conname + "'" in last:
                    return (definition,)
            return (READABLE_DOB_CONSTRAINT_DEF,)
        if "pg_trigger" in last:
            # The D20 audit_logs rung reads tgenabled, not a bare 1: 'O' (origin) is
            # what an enforcing trigger carries on a fully-migrated volume.
            return ("O",)
        if "'ssn_last4'" in last:
            # The D33 rung reads the column's rendered type via format_type, not a
            # bare presence marker -- a stub meant to model a fully-migrated database.
            return ("text",)
        return (1,)

    def fetchall(self):
        # The assistant_runs shape rung reads its column list via fetchall(); every other
        # rung on this fake uses fetchone(), so an unimplemented fetchall() here silently
        # returns {} -- every expected column reads as absent -- and /health goes 503 over
        # a stub meant to model a fully-migrated database.
        last = getattr(self, "_last", "")
        if "pg_attribute" in last:
            return list(ASSISTANT_RUNS_COLUMN_ROWS)
        return []


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


class _CaptureHandler(logging.Handler):
    """Collect records emitted on the origination logger. `logging_config.get_logger`
    sets `propagate = False`, so `caplog` reports "nothing was logged" for a line that
    WAS logged (same reason test_authz.py/test_llm_client.py carry their own copy)."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_llm_disabled_refusal_is_logged(monkeypatch):
    # Audit item 8: this 503 fires as a FastAPI dependency, before `_run_assistant` ever
    # opens `assistant.entry` -- with no fix, nothing records that the refusal happened.
    monkeypatch.delenv("LLM_ENABLED", raising=False)
    handler = _CaptureHandler()
    logger = logging.getLogger("origination")
    logger.addHandler(handler)
    try:
        with TestClient(app):
            with pytest.raises(HTTPException):
                get_llm_client(_Req(app, method="GET", path="/assistant/decisions/1"))
    finally:
        logger.removeHandler(handler)
    warnings = [r.getMessage() for r in handler.records if r.levelno >= logging.WARNING]
    assert warnings, "an LLM-disabled refusal must be logged"
    message = warnings[0]
    assert "GET" in message
    assert "/assistant/decisions/1" in message


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


def _origination_environment_block(compose: str = "docker-compose.yml") -> str:
    """origination-service's `environment:` mapping in `compose`.

    Defaults to the BASE compose file; the demo override is read by passing its name,
    rather than by a second copy of this reader.

    Asserts the stanza exists: a check that silently finds nothing must not report success
    over a file it never read.
    """
    lines = (REPO / compose).read_text().splitlines()
    starts = [i for i, line in enumerate(lines) if line == "  origination-service:"]
    assert starts, f"no `  origination-service:` stanza in {compose}"
    block = []
    for line in lines[starts[0] + 1 :]:
        if re.match(r"^  \S", line):  # next service key, same indent
            break
        block.append(line)
    assert block, f"origination-service stanza in {compose} is empty"
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


# --- boot does not check AWS credentials, and the demo override must not claim it does ----
#
# The demo override pins CLAUDE_PROVIDER=bedrock. Its comment claimed that enabling the LLM
# feature without an AWS credential "FAILS AT BOOT". It does not: load_llm_config() only
# requires CLAUDE_API_KEY on the `anthropic` path, and says so -- boto3 resolves Bedrock
# credentials at call time (env, profile, SSO, instance role), forms this function cannot
# detect from env vars. Startup then builds BedrockAdapter, whose SDK client is constructed
# lazily on first use. So the stack boots, /health returns 200 (it probes secrets and the
# database, never the model), and the first assistant call is where the missing credential
# surfaces -- mid-demo, which is the outcome the comment promised was impossible.
#
# Two tests, because the claim failed in two places: one pins the runtime behaviour, one
# pins the document that describes it.


def test_startup_succeeds_on_bedrock_without_aws_credential(monkeypatch):
    """provider=bedrock + no AWS credential => boot SUCCEEDS; the failure is at call time.

    This is a characterization test, not a regression test: the code already behaves this
    way and always did -- the compose comment was the thing that was wrong. Its job is to
    make the documented contract executable, so that adding a startup credential preflight
    (the other candidate fix) breaks a test that names the trade-off instead of silently
    changing when a stack is allowed to come up.
    """
    monkeypatch.setenv("LLM_ENABLED", "true")
    monkeypatch.setenv("CLAUDE_PROVIDER", "bedrock")
    # The exact configuration the comment named: an Anthropic key present, no AWS credential.
    monkeypatch.setenv("CLAUDE_API_KEY", "test-key")
    for var in (
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)

    with TestClient(app):
        assert isinstance(app.state.llm_client, ClaudeClient)
        assert app.state.llm_config.provider == "bedrock"


def test_demo_compose_does_not_claim_a_missing_credential_fails_at_boot():
    """The demo override may not tell an operator that boot proves the credential.

    Scoped to a credential claim on purpose: the AWS_REGION comment in the same block
    says a bad region is "refused at boot", which is TRUE (_aws_region raises inside
    load_llm_config), so a blanket ban on the words would fail an accurate line.
    """
    block = _origination_environment_block("docker-compose.demo.yml")
    comment = " ".join(
        line.split("#", 1)[1].strip() for line in block.splitlines() if "#" in line
    )

    for pattern in (
        r"credential[^.]*fails?\s+at\s+boot",
        r"fails?\s+at\s+boot[^.]*credential",
    ):
        assert not re.search(pattern, comment, re.I), (
            "docker-compose.demo.yml tells the reader that a missing AWS credential fails "
            "at boot. It does not -- load_llm_config() does not validate Bedrock "
            "credentials (app/llm/config.py), and BedrockAdapter builds its SDK client "
            "lazily, so the stack boots clean and dies on the first model call. An "
            "operator who trusts this comment treats `compose up` as the credential "
            "check and finds out in the room."
        )

    assert re.search(r"first\s+model\s+call", comment, re.I), (
        "docker-compose.demo.yml must name where a missing AWS credential actually "
        "surfaces (the first model call), so the pre-room check is the Bedrock proof "
        "run and not `compose up`"
    )
