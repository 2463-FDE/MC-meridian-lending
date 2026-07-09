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
    """Explicit local/demo mode: dev environment + opt-in flag + no key."""
    monkeypatch.setattr(config, "ENVIRONMENT", "development")
    monkeypatch.setattr(config, "ALLOW_SYNTHETIC_CREDIT", True)
    monkeypatch.setattr(config, "EXPERIAN_KEY", "")


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
