"""Decisioning tests.

The scorecard tests run in explicit SYNTHETIC-credit mode (ALLOW_SYNTHETIC_CREDIT):
there is no live Experian in the test environment, so `_pull_credit` uses its
deterministic stub (680 for an SSN ending in an even digit, 612 otherwise).
Persistence is best-effort and swallowed when no DB is present.

The fail-closed tests prove the security fix: with NO bureau key and synthetic mode
OFF (a production-like config), decision-service must NOT issue a decision, and
/health must report unhealthy — closing the "keyless deploy silently issues
decisions off a stub score" gap.

NOTE (intentional debt, left UNTESTED): there is deliberately NO test asserting a
decision audit trail / reason-code accuracy exists (D4, D10, twists #1/#2).
"""
import pytest

from app import config
from app.decision import decide, CreditPullError


@pytest.fixture
def synthetic_mode(monkeypatch):
    """Explicit local/demo mode: dev environment + opt-in flag + no key.

    Sets a password-bearing DATABASE_URL — the demo always runs against the
    compose Postgres, which requires a password — so readiness reflects a valid
    DB config, not the passwordless footgun."""
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


def test_clear_approve(synthetic_mode):
    # SSN ends in an even digit -> stub bureau score 680; high income clears.
    result = decide({"app_id": 1, "ssn": "123456782", "income": 100000})
    assert result["decision"] == "approve"
    assert result["score"] >= 660


def test_clear_deny(synthetic_mode):
    # SSN ends in an odd digit -> stub bureau score 612; zero income sinks it.
    result = decide({"app_id": 2, "ssn": "123456781", "income": 0})
    assert result["decision"] == "deny"
    assert result["score"] < 600


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


def test_health_ok_in_synthetic_mode(synthetic_mode):
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
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", True)
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:secret@postgres:5432/meridian"
    )
    assert config.database_url_configured() is True
    assert "DATABASE_URL" not in config.missing_required_secrets()


def test_decision_endpoint_returns_503_when_key_missing(prod_like):
    from fastapi.testclient import TestClient
    from app.main import app

    resp = TestClient(app).post(
        "/decisions",
        json={
            "application_id": 9, "applicant_id": 9, "name": "Test Applicant",
            "ssn": "123456782", "requested_amount": 15000, "term_months": 36,
            "annual_income": 100000, "monthly_debt": 0,
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
