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
        config, "DATABASE_URL", "postgresql://meridian:stale_old_pw@postgres:5432/meridian"
    )
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_password_matching_postgres_password_is_ok(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "the_real_pw")
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:the_real_pw@postgres:5432/meridian"
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


# --- Processor-key readiness + fail-closed capture -------------------------
# After the secret purge PROCESSOR_API_KEY has no committed fallback. Without it
# the service cannot authorize a real capture, so it must read unhealthy AND
# /payments must fail closed rather than record a 'captured' payment.


def test_missing_processor_key_flags_readiness(monkeypatch):
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "")
    assert "PROCESSOR_API_KEY" in config.missing_required_secrets()


def test_present_processor_key_not_flagged(monkeypatch):
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "proc_test")
    assert "PROCESSOR_API_KEY" not in config.missing_required_secrets()


def test_payments_503_without_processor_key(monkeypatch):
    # Call the route handler directly (no TestClient/httpx dependency): the guard
    # fires before charge(), so no DB is touched.
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "")
    from fastapi import HTTPException
    from app.routers.payments import post_payment
    from app.schemas import PaymentIn

    with pytest.raises(HTTPException) as exc_info:
        post_payment(PaymentIn(loan_id=1, amount=100.0))
    assert exc_info.value.status_code == 503


def test_payments_allowed_with_processor_key(monkeypatch):
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "proc_test")
    from app import payments

    monkeypatch.setattr(
        payments,
        "charge",
        lambda *a, **k: {
            "payment_id": 1, "loan_id": 1, "status": "captured", "applied_amount": 100.0
        },
    )
    from app.routers.payments import post_payment
    from app.schemas import PaymentIn

    out = post_payment(PaymentIn(loan_id=1, amount=100.0))
    assert out["status"] == "captured"
