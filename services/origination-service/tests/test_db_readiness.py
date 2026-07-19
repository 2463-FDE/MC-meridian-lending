"""DB readiness regression.

An unset, passwordless, placeholder, or stale/drifted DATABASE_URL must read as
misconfigured so /health can report unhealthy instead of connecting
unauthenticated or failing auth at first query. Covers the passwordless DSN
(meridian:@postgres) the secret purge left behind and the shipped placeholder.
"""

import threading
import time

import pytest

from app import config


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    # database_reachable caches its result; reset around every test so the probe
    # stubs below are observed fresh and cases don't leak into each other.
    config.reset_database_probe_cache()
    yield
    config.reset_database_probe_cache()


def test_unset_database_url_is_misconfigured(monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_passwordless_database_url_is_misconfigured(monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:@postgres:5432/meridian"
    )
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_placeholder_database_url_is_misconfigured(monkeypatch):
    # The .env.example placeholder has a non-empty password string but is not a
    # real credential — it must not read as healthy.
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql://meridian:REPLACE_WITH_POSTGRES_PASSWORD@postgres:5432/meridian",
    )
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_stale_password_rejected_against_postgres_password(monkeypatch):
    # DSN password drifted from POSTGRES_PASSWORD (source of truth) -> caught
    # without a DB round trip.
    monkeypatch.setenv("POSTGRES_PASSWORD", "the_real_pw")
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql://meridian:stale_old_pw@postgres:5432/meridian",
    )
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_password_matching_postgres_password_is_ok(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "the_real_pw")
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql://meridian:the_real_pw@postgres:5432/meridian",
    )
    assert config.database_url_configured() is True
    assert "DATABASE_URL" not in config.missing_required_secrets()


def test_password_bearing_database_url_is_ok(monkeypatch):
    # No POSTGRES_PASSWORD reference (e.g. external managed DB) -> a real,
    # non-placeholder password is accepted.
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    assert config.database_url_configured() is True
    assert "DATABASE_URL" not in config.missing_required_secrets()


def test_url_encoded_reserved_char_password_is_ok(monkeypatch):
    # A password with reserved URL chars must be percent-encoded in the DSN
    # (p@ss/word:1 -> p%40ss%2Fword%3A1); the gate must decode before comparing
    # to POSTGRES_PASSWORD, else a valid password is falsely flagged stale.
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/word:1")
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql://meridian:p%40ss%2Fword%3A1@postgres:5432/meridian",
    )
    assert config.database_url_configured() is True
    assert "DATABASE_URL" not in config.missing_required_secrets()


# --- Live connectivity probe (database_reachable) --------------------------
# The config gate above cannot prove a password authenticates. database_reachable
# opens a bounded connection and runs SELECT 1; these tests stub psycopg2.connect
# so no real database is required.


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
    def __init__(self):
        self.closed_flag = False

    def cursor(self):
        return _FakeCursor()

    def close(self):
        self.closed_flag = True


def test_probe_ok_when_connection_succeeds(monkeypatch):
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(config.psycopg2, "connect", lambda *a, **k: _FakeConn())
    ok, err = config.database_reachable()
    assert ok is True
    assert err is None


def test_probe_fails_on_wrong_password_without_postgres_password(monkeypatch):
    # The documented residual the config gate cannot catch: a real, non-placeholder
    # DSN password with no POSTGRES_PASSWORD to compare against. database_url_configured
    # accepts it; only the live probe detects that it does not authenticate.
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:wrong_pw@postgres:5432/meridian"
    )
    assert config.database_url_configured() is True  # config gate cannot tell

    def _auth_fail(*a, **k):
        raise config.psycopg2.OperationalError("password authentication failed")

    monkeypatch.setattr(config.psycopg2, "connect", _auth_fail)
    ok, err = config.database_reachable()
    assert ok is False
    assert err == "OperationalError"  # class name only — no DSN/password leak


class _SchemaMissingCursor:
    """Connects and answers SELECT 1, but reports the required schema absent — the
    unmigrated-volume case (applications.monthly_debt not yet applied)."""

    def __init__(self):
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *a, **k):
        self._last = sql

    def fetchone(self):
        return None if "information_schema" in self._last else (1,)


class _SchemaMissingConn:
    def cursor(self):
        return _SchemaMissingCursor()

    def close(self):
        pass


def test_probe_fails_when_schema_not_migrated(monkeypatch):
    # PR review: a reachable DB whose volume predates 0006 would 500 the decision path
    # on the monthly_debt SELECT. The probe must report readiness FALSE, naming the
    # missing column, so /health shows unhealthy instead of a silent decisioning break.
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(
        config.psycopg2, "connect", lambda *a, **k: _SchemaMissingConn()
    )
    ok, err = config.database_reachable()
    assert ok is False
    assert err == "schema_not_ready:applications.monthly_debt"


class _LoansIndexMissingCursor:
    """Column present but the uq_loans_app unique index absent — a partially-applied
    migration that would let concurrent accepts board duplicate loans (PR review)."""

    def __init__(self):
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *a, **k):
        self._last = sql

    def fetchone(self):
        # information_schema (monthly_debt column) passes; pg_indexes (index) fails.
        return None if "pg_indexes" in self._last else (1,)


class _LoansIndexMissingConn:
    def cursor(self):
        return _LoansIndexMissingCursor()

    def close(self):
        pass


def test_probe_fails_when_loans_unique_index_missing(monkeypatch):
    # PR review: idempotent boarding relies on uq_loans_app; a partially-applied
    # migration with the loans table but no index would let concurrent accepts board
    # duplicate loans. Readiness must fail on that state, naming the missing index.
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(
        config.psycopg2, "connect", lambda *a, **k: _LoansIndexMissingConn()
    )
    ok, err = config.database_reachable()
    assert ok is False
    assert err == "schema_not_ready:uq_loans_app"


class _ContinuationTokenMissingCursor:
    """monthly_debt column + uq_loans_app index present, but the ADR 0010 Phase B
    continuation_token column absent -- a volume predating migration 0008, on which submit
    could not issue a token and anonymous apply would silently break."""

    def __init__(self):
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *a, **k):
        self._last = sql

    def fetchone(self):
        if "continuation_token" in self._last:
            return None
        return (1,)


class _ContinuationTokenMissingConn:
    def cursor(self):
        return _ContinuationTokenMissingCursor()

    def close(self):
        pass


def test_probe_fails_when_continuation_token_column_missing(monkeypatch):
    # PR review: the public apply flow authorizes on applications.continuation_token; a
    # volume without the column must report unhealthy, naming it, not 500 submit silently.
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(
        config.psycopg2, "connect", lambda *a, **k: _ContinuationTokenMissingConn()
    )
    ok, err = config.database_reachable()
    assert ok is False
    assert err == "schema_not_ready:applications.continuation_token"


class _ExpiresAtMissingCursor:
    """continuation_token present but continuation_token_expires_at absent -- a volume
    predating migration 0009, on which authz's token SELECT and submit's INSERT would 500
    while /health looked fine (PR #7 review)."""

    def __init__(self):
        self._last = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, *a, **k):
        self._last = sql

    def fetchone(self):
        if "'continuation_token_expires_at'" in self._last:
            return None
        return (1,)


class _ExpiresAtMissingConn:
    def cursor(self):
        return _ExpiresAtMissingCursor()

    def close(self):
        pass


def test_probe_fails_when_continuation_token_expires_at_column_missing(monkeypatch):
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(
        config.psycopg2, "connect", lambda *a, **k: _ExpiresAtMissingConn()
    )
    ok, err = config.database_reachable()
    assert ok is False
    assert err == "schema_not_ready:applications.continuation_token_expires_at"


def test_probe_false_when_database_url_unset(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    ok, err = config.database_reachable()
    assert ok is False


def test_probe_passes_bounded_timeouts_and_closes(monkeypatch):
    captured = {}
    conn = _FakeConn()

    def _capture(dsn, **kwargs):
        captured.update(kwargs)
        return conn

    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(config.psycopg2, "connect", _capture)
    config.database_reachable()
    assert captured["connect_timeout"] >= 1
    assert "statement_timeout" in captured["options"]
    assert conn.closed_flag is True  # connection is always closed


def test_probe_result_is_cached_within_ttl(monkeypatch):
    # /health must not open a Postgres connection per request. Two calls within the
    # TTL must reuse one connection — the DoS-amplifier fix.
    calls = {"n": 0}

    def _count(*a, **k):
        calls["n"] += 1
        return _FakeConn()

    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(config.psycopg2, "connect", _count)
    config.database_reachable()
    config.database_reachable()
    assert calls["n"] == 1  # second call served from cache, no new connection


def test_probe_single_flight_under_concurrent_misses(monkeypatch):
    # N threads hit a cold cache simultaneously; single-flight must collapse them
    # to ONE psycopg2.connect, not one connection per request (the /health-flood fix).
    calls = {"n": 0}
    count_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def _slow_connect(*a, **k):
        with count_lock:
            calls["n"] += 1
        time.sleep(0.05)  # hold the probe so all threads pile onto the miss path
        return _FakeConn()

    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(config.psycopg2, "connect", _slow_connect)

    results = []

    def worker():
        barrier.wait()  # release all threads at once -> simultaneous cold-cache miss
        results.append(config.database_reachable())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1  # exactly one probe despite 8 concurrent misses
    assert results == [(True, None)] * 8
