"""Decisioning tests.

The scorecard tests run in explicit SYNTHETIC-credit mode (ALLOW_SYNTHETIC_CREDIT):
there is no live Experian in the test environment, so `_pull_credit` uses its
deterministic stub (680 for an SSN ending in an even digit, 612 otherwise).
Persistence is now MANDATORY (fail closed, ADR 0009 §4) — these tests stub
app.decision.db.query so no live Postgres is required.

The fail-closed tests prove the security fix: with NO bureau key and synthetic mode
OFF (a production-like config), decision-service must NOT issue a decision, and
/health must report unhealthy — closing the "keyless deploy silently issues
decisions off a stub score" gap.

The decision audit trail / reason-code contract (formerly intentional untested debt
D4/D10) is covered in test_decision_record.py.
"""

import threading
import time

import pytest

from app import config
from app import decision as decision_mod
from app.decision import decide, CreditPullError


@pytest.fixture
def event_sink(monkeypatch):
    """Swallow the mandatory decision-event write (no Postgres in unit tests)."""
    monkeypatch.setattr(decision_mod.db, "query", lambda sql, params=None: [])


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    # database_reachable caches its result; reset around every test so probe stubs
    # are observed fresh and /health cases don't leak into each other.
    config.reset_database_probe_cache()
    yield
    config.reset_database_probe_cache()


@pytest.fixture
def synthetic_mode(monkeypatch):
    """Explicit local/demo mode: dev environment + opt-in flag + no key.

    Sets a password-bearing DATABASE_URL — the demo always runs against the
    compose Postgres, which requires a password — so readiness reflects a valid
    DB config, not the passwordless footgun. POSTGRES_PASSWORD is cleared so the
    DSN-consistency check is skipped (the DSN password alone is validated)."""
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", True)
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:pw@postgres:5432/meridian"
    )


@pytest.fixture
def prod_like(monkeypatch):
    """Production-like: no key, synthetic NOT allowed."""
    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", False)
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")


@pytest.fixture
def env_example_semantics(monkeypatch):
    """Mirror a fresh copy of .env.example: ENVIRONMENT=production, the synthetic
    flag UNSET, and no EXPERIAN_KEY — the default a deploy inherits."""
    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", False)
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")


def test_clear_approve(synthetic_mode, event_sink):
    # SSN ends in an even digit -> stub bureau score 680; high income clears.
    result = decide(
        {
            "app_id": 1,
            "ssn": "123456782",
            "income": 100000,
            "amount": 15000,
            "term_months": 36,
            "employment_years": 5,
        }
    )
    assert result["decision"] == "approve"
    assert result["score"] >= 660


def test_clear_deny(synthetic_mode, event_sink):
    # SSN ends in an odd digit -> stub bureau score 612; zero income sinks it.
    result = decide(
        {
            "app_id": 2,
            "ssn": "123456781",
            "income": 0,
            "amount": 15000,
            "term_months": 36,
        }
    )
    assert result["decision"] == "deny"
    assert result["score"] < 600
    # Adverse action now carries specific principal reasons, never the generic string.
    assert result["principal_reasons"]
    assert all(
        "purchasing history" not in r["reason"].lower()
        for r in result["principal_reasons"]
    )


def test_missing_key_fails_closed_no_decision(prod_like):
    # Production-like config with no bureau key must NOT return a decision.
    with pytest.raises(CreditPullError):
        decide({"app_id": 3, "ssn": "123456782", "income": 100000})


def test_health_reports_unhealthy_when_bureau_key_missing(prod_like):
    from fastapi.testclient import TestClient
    from app.main import app

    resp = TestClient(app).get("/health")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert "EXPERIAN_KEY" in body["missing_secrets"]


def test_health_ok_in_synthetic_mode(synthetic_mode, monkeypatch):
    # /health now also runs a live DB probe; stub it as reachable so this exercises
    # the config-readiness path (a real run reaches the compose Postgres). _FakeConn
    # is defined below at module scope, so it resolves at call time.
    monkeypatch.setattr(config.psycopg2, "connect", lambda *a, **k: _FakeConn())

    from fastapi.testclient import TestClient
    from app.main import app

    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_health_flags_passwordless_database_url(monkeypatch):
    # The secret purge replaced a committed DB password with a passwordless DSN
    # (meridian:@postgres). It LOOKS configured but authenticates with no
    # password, so readiness must flag it rather than report OK. Synthetic mode
    # (bureau key not required) isolates the DATABASE_URL check.
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", True)
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:@postgres:5432/meridian"
    )
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()

    from fastapi.testclient import TestClient
    from app.main import app

    resp = TestClient(app).get("/health")
    assert resp.status_code == 503
    assert "DATABASE_URL" in resp.json()["missing_secrets"]


def test_health_flags_unset_database_url(monkeypatch):
    # An entirely unset DATABASE_URL (no committed default anymore) must also
    # read as misconfigured, not silently fall back to a usable-looking DSN.
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", True)
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")
    monkeypatch.setattr(config, "DATABASE_URL", "")
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_health_ok_with_password_bearing_database_url(monkeypatch):
    # A DSN with a real password clears the DB readiness check.
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", True)
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:secret@postgres:5432/meridian"
    )
    assert config.database_url_configured() is True
    assert "DATABASE_URL" not in config.missing_required_secrets()


def test_health_flags_placeholder_database_url(monkeypatch):
    # The .env.example placeholder has a non-empty password string but is not a
    # real credential; readiness must flag it rather than report OK (else
    # docker-compose health passes and the first real query fails auth).
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", True)
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql://meridian:REPLACE_WITH_POSTGRES_PASSWORD@postgres:5432/meridian",
    )
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_health_flags_database_url_password_drift(monkeypatch):
    # DSN password inconsistent with POSTGRES_PASSWORD (rotated/stale) is caught
    # without a DB round trip — this is the placeholder/stale case generalized.
    monkeypatch.setenv("POSTGRES_PASSWORD", "the_real_pw")
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", True)
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql://meridian:stale_old_pw@postgres:5432/meridian",
    )
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_database_url_encoded_reserved_char_password_is_ok(monkeypatch):
    # A reserved-char password must be percent-encoded in the DSN
    # (p@ss/word:1 -> p%40ss%2Fword%3A1); the gate decodes before comparing to
    # POSTGRES_PASSWORD, so a valid encoded password is not falsely flagged stale.
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/word:1")
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", True)
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql://meridian:p%40ss%2Fword%3A1@postgres:5432/meridian",
    )
    assert config.database_url_configured() is True
    assert "DATABASE_URL" not in config.missing_required_secrets()


# --- Live connectivity probe (database_reachable) --------------------------
# database_url_configured cannot prove the password authenticates. The live probe
# opens a bounded connection and runs SELECT 1; these tests stub psycopg2.connect
# so no real database is required. Named with "database_url" so the CI readiness
# gate (pytest -k database_url) selects them.


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


def test_database_url_probe_ok_when_connection_succeeds(monkeypatch):
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(config.psycopg2, "connect", lambda *a, **k: _FakeConn())
    ok, err = config.database_reachable()
    assert ok is True
    assert err is None


def test_database_url_probe_fails_on_wrong_password_without_postgres_password(
    monkeypatch,
):
    # The documented residual the config gate cannot catch: a real, non-placeholder
    # DSN password with no POSTGRES_PASSWORD to compare against. The gate accepts it;
    # only the live probe detects that it does not authenticate.
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


def test_database_url_probe_result_is_cached_within_ttl(monkeypatch):
    # /health must not open a Postgres connection per request; two calls within the
    # TTL reuse one connection (the DoS-amplifier fix).
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
    assert calls["n"] == 1


def test_database_url_probe_single_flight_under_concurrent_misses(monkeypatch):
    # N threads hit a cold cache at once; single-flight must collapse them to ONE
    # psycopg2.connect, not one connection per request (the /health-flood fix).
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
        barrier.wait()
        results.append(config.database_reachable())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1
    assert results == [(True, None)] * 8


def test_health_flags_unreachable_database_url(monkeypatch):
    # config gate passes (real password, synthetic mode isolates the DB check) but
    # the DB rejects auth; the live probe must drive /health to 503, not report ok.
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", True)
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:wrong_pw@postgres:5432/meridian"
    )
    assert config.missing_required_secrets() == []

    def _auth_fail(*a, **k):
        raise config.psycopg2.OperationalError("password authentication failed")

    monkeypatch.setattr(config.psycopg2, "connect", _auth_fail)

    from fastapi.testclient import TestClient
    from app.main import app

    resp = TestClient(app).get("/health")
    assert resp.status_code == 503
    assert resp.json()["database_error"] == "OperationalError"


def test_decision_endpoint_returns_503_when_key_missing(prod_like):
    from fastapi.testclient import TestClient
    from app.main import app

    resp = TestClient(app).post(
        "/decisions",
        json={
            "application_id": 9,
            "applicant_id": 9,
            "name": "Test Applicant",
            "ssn": "123456782",
            "requested_amount": 15000,
            "term_months": 36,
            "annual_income": 100000,
            "monthly_debt": 0,
        },
    )
    assert resp.status_code == 503  # fail closed — no decision issued


def test_synthetic_flag_ignored_outside_development(monkeypatch):
    # Two-gate guard: the opt-in flag alone must NOT enable synthetic scoring in a
    # production environment — no config can approve loans on fake data by accident.
    monkeypatch.setattr(config, "ENVIRONMENT", "production")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", True)  # set, but env is prod
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")
    assert config.synthetic_credit_enabled() is False
    assert "EXPERIAN_KEY" in config.missing_required_secrets()
    with pytest.raises(CreditPullError):
        decide({"app_id": 4, "ssn": "123456782", "income": 100000})


def test_env_example_semantics_report_unhealthy(env_example_semantics):
    # A fresh copy of .env.example (no key, synthetic off, production) must leave
    # decision-service unhealthy — a copied config cannot silently issue decisions.
    from fastapi.testclient import TestClient
    from app.main import app

    resp = TestClient(app).get("/health")
    assert resp.status_code == 503
    assert "EXPERIAN_KEY" in resp.json()["missing_secrets"]


def _parse_env_example() -> list:
    """Parse the repo .env.example into (key, value) pairs, mirroring dotenv:
    uncommented KEY=VALUE lines only, in file order (so a later duplicate wins,
    which is exactly the last-assignment-wins semantics a deploy inherits)."""
    from pathlib import Path

    # tests/ -> decision-service/ -> services/ -> repo root
    env_path = Path(__file__).resolve().parents[3] / ".env.example"
    pairs = []
    for line in env_path.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        key, _, value = s.partition("=")
        pairs.append((key.strip(), value.strip()))
    return pairs


def test_env_example_gate_stays_closed_by_parsing_the_template():
    """Regression: parse the REAL .env.example instead of monkeypatching its
    assumed semantics. A duplicate ENVIRONMENT=development (later line wins in
    dotenv/compose) silently reopened the synthetic-credit gate a copied deploy
    inherits. Assert the template defines ENVIRONMENT exactly once, its value is
    not "development", and it ships no ALLOW_SYNTHETIC_CREDIT default."""
    pairs = _parse_env_example()
    env_values = [v for k, v in pairs if k == "ENVIRONMENT"]
    assert len(env_values) == 1, f"expected one ENVIRONMENT, got {env_values}"
    # Effective (last-wins) value must not open the dev gate.
    assert env_values[-1].strip().lower() != "development"
    # The synthetic escape hatch must not be defaulted on in the template.
    assert not any(k == "ALLOW_SYNTHETIC_CREDIT" for k, _ in pairs)
