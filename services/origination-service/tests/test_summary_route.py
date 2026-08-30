"""The officer loan-summary route (owed item 2) and its payload builder.

Offline: the model is a FakeAdapter, and `summary_payload`'s DB read is stubbed. What is
proved is the wiring — officer-only authz, 404 on an unknown app, clean 503s when the
feature is off or the provider fails, and (the test that carries weight) that the payload
handed to the model selects ZERO identity columns.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.llm import ClaudeClient, FakeAdapter
from app.llm.config import LLMConfig
from app.llm.errors import LLMHTTPError
from app.routers import applications

OFFICER = {"X-User-Role": "underwriter"}
BORROWER = {"X-User-Role": "borrower"}

SUMMARY = {
    "summary": "Applicant requests $18,000 over 48 months for an auto purchase.",
    "risk_flags": ["short employment tenure"],
    "recommended_next_step": "request_docs",
}

PAYLOAD = {
    "amount": 18000.0,
    "term_months": 48,
    "purpose": "auto",
    "income": 42000.0,
    "monthly_debt": 300.0,
    "status": "submitted",
    "outcome": "approve",
}


def _wire(monkeypatch, *, llm=True, raises=None):
    if llm:
        cfg = LLMConfig(
            api_key="test-key", model="claude-test", max_tokens=256, max_retries=0
        )
        adapter = FakeAdapter(response=json.dumps(SUMMARY), raises=raises)
        client = ClaudeClient(cfg, adapter=adapter)
    else:
        client = None
    monkeypatch.setattr(main.app.state, "llm_client", client, raising=False)
    return TestClient(main.app)


def test_happy_path_returns_all_three_schema_keys(monkeypatch):
    monkeypatch.setattr(applications, "summary_payload", lambda app_id: dict(PAYLOAD))
    client = _wire(monkeypatch)
    resp = client.get("/applications/1/summary", headers=OFFICER)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"summary", "risk_flags", "recommended_next_step"}
    assert body["recommended_next_step"] == "request_docs"


def test_borrower_is_forbidden(monkeypatch):
    monkeypatch.setattr(applications, "summary_payload", lambda app_id: dict(PAYLOAD))
    client = _wire(monkeypatch)
    assert client.get("/applications/1/summary", headers=BORROWER).status_code == 403


def test_unknown_application_is_404(monkeypatch):
    monkeypatch.setattr(applications, "summary_payload", lambda app_id: None)
    client = _wire(monkeypatch)
    assert client.get("/applications/999/summary", headers=OFFICER).status_code == 404


def test_feature_disabled_is_503(monkeypatch):
    monkeypatch.setattr(applications, "summary_payload", lambda app_id: dict(PAYLOAD))
    client = _wire(monkeypatch, llm=False)
    assert client.get("/applications/1/summary", headers=OFFICER).status_code == 503


def test_provider_failure_is_503_not_500(monkeypatch):
    monkeypatch.setattr(applications, "summary_payload", lambda app_id: dict(PAYLOAD))
    err = LLMHTTPError("boom", status_code=503, retryable=False)
    client = _wire(monkeypatch, raises=[err])
    resp = client.get("/applications/1/summary", headers=OFFICER)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "summary unavailable"


def test_the_11th_call_in_a_window_is_rate_limited(monkeypatch):
    # RL-001 (PR review): this route invokes Bedrock but never called the limiter --
    # an authenticated officer had unbounded calls through it. Exercises the real
    # counting/window logic (not mocked), unlike the sibling wiring test below.
    from app import rate_limit

    monkeypatch.setattr(applications, "summary_payload", lambda app_id: dict(PAYLOAD))
    client = _wire(monkeypatch)
    for _ in range(rate_limit._MAX_CALLS):
        resp = client.get("/applications/1/summary", headers=OFFICER)
        assert resp.status_code == 200, resp.text
    resp = client.get("/applications/1/summary", headers=OFFICER)
    assert resp.status_code == 429


def test_summary_route_surfaces_the_rate_limit(monkeypatch):
    # Wiring only, mirrors test_assistant_route_surfaces_the_rate_limit.
    from fastapi import HTTPException

    monkeypatch.setattr(applications, "summary_payload", lambda app_id: dict(PAYLOAD))

    def _tripped(user_id):
        raise HTTPException(status_code=429, detail="assistant rate limit exceeded")

    monkeypatch.setattr(main.rate_limit, "check_llm_rate_limit", _tripped)
    client = _wire(monkeypatch)
    resp = client.get("/applications/1/summary", headers=OFFICER)
    assert resp.status_code == 429


# --- summary_payload: the payload the model actually receives ---

_IDENTITY_KEYS = {"name", "ssn", "email", "phone", "address", "dob"}


def _stub_query(monkeypatch, rows):
    def query(sql, params=None):
        # Guard against a future edit that reintroduces an applicants join.
        assert "applicants" not in sql.lower(), (
            "summary_payload must never join applicants"
        )
        return rows

    monkeypatch.setattr(applications.db, "query", query)


def test_payload_carries_no_identity_keys(monkeypatch):
    _stub_query(
        monkeypatch,
        [
            {
                "amount": 18000.0,
                "term_months": 48,
                "purpose": "auto",
                "income": 42000.0,
                "monthly_debt": 300.0,
                "employer": "Acme",
                "job_title": "Tech",
                "employment_years": 5.0,
                "status": "submitted",
                "apr": 9.5,
                "finance_charge": 3600.0,
                "monthly_payment": 439.0,
                "amount_financed": 17460.0,
                "total_of_payments": 21088.0,
                "outcome": "approve",
                "name_verified": True,
                "dob_verified": True,
                "address_verified": True,
                "ssn_verified": True,
            }
        ],
    )
    payload = applications.summary_payload(1)
    assert _IDENTITY_KEYS.isdisjoint(payload), payload
    assert payload["amount"] == 18000.0


def test_missing_application_returns_none(monkeypatch):
    _stub_query(monkeypatch, [])
    assert applications.summary_payload(999) is None


def test_null_fields_are_omitted_not_422(monkeypatch):
    # A NULL monthly_debt (and an absent offer/decision/kyc) must not 422 or surface a
    # bare null — the advisory summary simply omits the key.
    _stub_query(
        monkeypatch,
        [
            {
                "amount": 18000.0,
                "term_months": 48,
                "purpose": None,
                "income": None,
                "monthly_debt": None,
                "employer": None,
                "job_title": None,
                "employment_years": None,
                "status": "submitted",
                "apr": None,
                "finance_charge": None,
                "monthly_payment": None,
                "amount_financed": None,
                "total_of_payments": None,
                "outcome": None,
                "name_verified": None,
                "dob_verified": None,
                "address_verified": None,
                "ssn_verified": None,
            }
        ],
    )
    payload = applications.summary_payload(1)
    assert payload == {"amount": 18000.0, "term_months": 48, "status": "submitted"}
    assert "monthly_debt" not in payload
