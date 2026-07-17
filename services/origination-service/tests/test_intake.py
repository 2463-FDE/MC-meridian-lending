"""Intake persistence of model inputs (PR #7 review).

The LOS must persist the real underwriting inputs (income, monthly_debt,
employment_years) and forward them to decision-service — not fabricate zeros. DB is
stubbed (no live Postgres in unit tests; the column + end-to-end write are exercised by
the smoke test against the compose stack).
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app import intake
from app.main import app
from app.routers import applications


def test_create_application_persists_model_inputs(monkeypatch):
    captured = []

    def _query(sql, params=None):
        captured.append((sql, params))
        return [{"id": 1}]  # applicant id, then application id

    monkeypatch.setattr(intake.db, "query", _query)
    intake.create_application(
        {
            "name": "Test Borrower",
            "amount": 15000,
            "term_months": 36,
            "purpose": "debt_consolidation",
            "income": 65000,
            "monthly_debt": 500,
            "employment_years": 3,
        }
    )
    app_insert = next(c for c in captured if "INSERT INTO applications" in c[0])
    # (applicant_id, amount, term_months, purpose, income, monthly_debt, employment_years)
    assert app_insert[1] == (1, 15000, 36, "debt_consolidation", 65000, 500, 3)


def test_decision_request_payload_uses_captured_inputs(monkeypatch):
    row = {
        "applicant_id": 9,
        "amount": 15000,
        "term_months": 36,
        "income": 65000,
        "monthly_debt": 500,
        "employment_years": 3,
        "name": "Test Borrower",
        "ssn": "123456789",
    }
    monkeypatch.setattr(applications.db, "query", lambda sql, params=None: [row])
    payload = applications.decision_request_payload(1)
    # The exact inputs the decision model will score — no fabricated zeros.
    assert payload["annual_income"] == 65000
    assert payload["monthly_debt"] == 500
    assert payload["employment_years"] == 3


def test_persisted_null_monthly_debt_is_quarantined_not_scored_as_zero(monkeypatch):
    # PR #7 review: a persisted row with NULL monthly_debt (legacy / seeded / non-API
    # write) must NOT be decisioned as debt-free. decision_request_payload fails closed
    # with 422 instead of defaulting to 0, so the bad input never reaches the model or
    # the append-only decision event.
    from fastapi import HTTPException

    row = {
        "applicant_id": 9,
        "amount": 15000,
        "term_months": 36,
        "income": 50000,
        "monthly_debt": None,  # legacy row: no recorded debt
        "employment_years": 3,
        "name": "Legacy",
        "ssn": "123456789",
    }
    monkeypatch.setattr(applications.db, "query", lambda sql, params=None: [row])
    with pytest.raises(HTTPException) as exc:
        applications.decision_request_payload(1)
    assert exc.value.status_code == 422


def test_null_income_and_employment_still_fall_back(monkeypatch):
    # Only monthly_debt is quarantined (its zero over-approves). income/employment
    # omission fails toward denial, so they retain the 0 fallback.
    row = {
        "applicant_id": 9,
        "amount": 15000,
        "term_months": 36,
        "income": None,
        "monthly_debt": 500,  # present -> not quarantined
        "employment_years": None,
        "name": "X",
        "ssn": "123456789",
    }
    monkeypatch.setattr(applications.db, "query", lambda sql, params=None: [row])
    payload = applications.decision_request_payload(1)
    assert payload["monthly_debt"] == 500
    assert payload["annual_income"] == 0
    assert payload["employment_years"] == 0


def _kyc_test_db(audit_sink):
    """db.query fake for submit_application: answers the applicant_id SELECT and records
    any audit_logs INSERT into audit_sink."""

    def _query(sql, params=None):
        if "audit_logs" in sql:
            audit_sink.append(params)
            return []
        return [{"applicant_id": 9}]  # SELECT applicant_id FROM applications

    return _query


def test_kyc_transport_failure_is_observable_not_silent(monkeypatch):
    # PR review: a KYC transport/auth failure (outage, timeout, or a mis/rotated internal
    # token -> 403) must NOT masquerade as an ordinary all-false verification. Intake
    # resilience is kept (still 200/submitted), but the failure is made observable:
    # kyc_checked=False in the response AND an audit_logs row.
    monkeypatch.setattr(applications.intake, "create_application", lambda payload: 1)
    audit = []
    monkeypatch.setattr(applications.db, "query", _kyc_test_db(audit))

    def _kyc_down(*a, **k):
        raise httpx.ConnectError("kyc-service unreachable")

    monkeypatch.setattr(applications.clients, "post", _kyc_down)
    resp = TestClient(app).post(
        "/applications",
        json={"name": "Test Borrower", "amount": 10000, "monthly_debt": 500},
    )
    assert resp.status_code == 200  # resilience kept — intake not 500'd by a KYC hiccup
    body = resp.json()
    assert body["status"] == "submitted"
    assert body["kyc_checked"] is False  # observable: the check did not run
    assert all(v is False for v in body["kyc"].values())
    assert len(audit) == 1  # a kyc_unavailable audit row was recorded
    actor, action, detail = audit[0]
    assert action == "kyc_unavailable"
    assert "app_id=1" in detail
    assert "ConnectError" in detail  # error class only — no PII


def test_kyc_success_sets_kyc_checked_true(monkeypatch):
    # Contrast: when KYC actually runs, kyc_checked is True and a genuine decline is a
    # 200 with cip_passed False (no audit row) — distinct from the failure path above.
    monkeypatch.setattr(applications.intake, "create_application", lambda payload: 1)
    audit = []
    monkeypatch.setattr(applications.db, "query", _kyc_test_db(audit))
    monkeypatch.setattr(
        applications.clients, "post", lambda *a, **k: {"cip_passed": True}
    )
    resp = TestClient(app).post(
        "/applications",
        json={"name": "Test Borrower", "amount": 10000, "monthly_debt": 500},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kyc_checked"] is True
    assert body["kyc"]["name_verified"] is True
    assert audit == []  # no failure audit row on the success path


def test_api_rejects_missing_monthly_debt():
    # PR #7 review: omitting monthly_debt must be rejected at the API boundary (422)
    # before any intake/decisioning runs — not silently persisted as NULL and scored
    # as zero debt. FastAPI validates the body before the handler, so no downstream
    # stubbing is needed (the handler never executes).
    resp = TestClient(app).post(
        "/applications",
        json={"name": "Test Borrower", "amount": 10000, "term_months": 36},
    )
    assert resp.status_code == 422
    # explicit 0 is accepted (would proceed past validation into the handler)
    body = resp.json()
    assert any("monthly_debt" in str(err.get("loc", "")) for err in body["detail"])


def test_officer_decision_route_422s_on_persisted_null_debt(monkeypatch):
    # PR #7 review regression: a persisted application with NULL monthly_debt cannot be
    # decisioned as zero — the officer route returns 422 before any decision-service call.
    null_row = {
        "applicant_id": 9,
        "amount": 15000,
        "term_months": 36,
        "income": 50000,
        "monthly_debt": None,
        "employment_years": 3,
        "name": "Legacy",
        "ssn": "123456789",
    }
    monkeypatch.setattr(applications.db, "query", lambda sql, params=None: [null_row])

    def _must_not_call(*a, **k):
        raise AssertionError("decision-service must not be called for a NULL-debt row")

    monkeypatch.setattr(applications.clients, "post", _must_not_call)
    resp = TestClient(app).post("/applications/1/decision")
    assert resp.status_code == 422


def _stateful_db(row):
    """Fake db.query backed by a mutable row: a SELECT for the matching app_id returns
    the current row (else [] for an unknown app_id), UPDATE ... monthly_debt mutates it.
    Models the capture-then-decision remediation cycle. Both the capture endpoint's
    existence check and decision_request_payload look the app up by id as params[0]."""

    def _query(sql, params=None):
        if sql.strip().upper().startswith("UPDATE"):
            new_debt, app_id = params
            if app_id != row["id"]:
                return []  # no such application
            row["monthly_debt"] = new_debt
            return [{"id": row["id"]}]
        if params and params[0] != row["id"]:
            return []  # SELECT for an unknown app_id
        return [row]  # SELECT (capture existence check / decision_request_payload)

    return _query


def test_capture_monthly_debt_unblocks_officer_decision(monkeypatch):
    # Remediation-path regression: a legacy row with NULL monthly_debt is quarantined
    # (422) at decisioning; after POST /monthly-debt captures the value, the same
    # officer route decisions normally. This is the intended recovery from quarantine.
    row = {
        "id": 1,
        "applicant_id": 9,
        "amount": 15000,
        "term_months": 36,
        "income": 50000,
        "monthly_debt": None,  # legacy row: quarantined
        "employment_years": 3,
        "name": "Legacy",
        "ssn": "123456789",
    }
    monkeypatch.setattr(applications.db, "query", _stateful_db(row))
    monkeypatch.setattr(
        applications.clients,
        "post",
        lambda *a, **k: {"outcome": "approve", "score": 700, "reason": None},
    )
    # The remediation endpoint is internal-only: configure the token and send the
    # X-Internal-Service header (PR review).
    monkeypatch.setattr(applications.config, "INTERNAL_SERVICE_TOKEN", "tok")
    client = TestClient(app)

    # Before capture: quarantined.
    assert client.post("/applications/1/decision").status_code == 422

    # Capture the missing value.
    patched = client.post(
        "/applications/1/monthly-debt",
        json={"monthly_debt": 450},
        headers={"X-Internal-Service": "tok"},
    )
    assert patched.status_code == 200
    assert patched.json()["monthly_debt"] == 450

    # After capture: decisionable.
    decided = client.post("/applications/1/decision")
    assert decided.status_code == 200
    assert decided.json()["decision"] == "approve"


def test_capture_monthly_debt_rejects_negative():
    # Same ge=0 rule as the submit boundary — a negative debt is a 422, not persisted.
    resp = TestClient(app).post(
        "/applications/1/monthly-debt", json={"monthly_debt": -1}
    )
    assert resp.status_code == 422


def test_capture_monthly_debt_404_when_missing(monkeypatch):
    # Existence check finds no row for an unknown app_id -> 404, not a silent 200.
    row = {"id": 1, "monthly_debt": None}
    monkeypatch.setattr(applications.db, "query", _stateful_db(row))
    monkeypatch.setattr(applications.config, "INTERNAL_SERVICE_TOKEN", "tok")
    resp = TestClient(app).post(
        "/applications/999/monthly-debt",
        json={"monthly_debt": 100},
        headers={"X-Internal-Service": "tok"},
    )
    assert resp.status_code == 404


def test_board_endpoint_is_internal_only(monkeypatch):
    # PR review: the legacy /board endpoint creates a loan + balance from fully
    # caller-supplied inputs and is reachable via the anonymous /los proxy. An
    # anonymous caller must be refused before board_to_servicing runs.
    monkeypatch.setattr(applications.config, "INTERNAL_SERVICE_TOKEN", "tok")

    def _explode(*a, **k):
        raise AssertionError(
            "board_to_servicing must not run for an unauthorized /board"
        )

    monkeypatch.setattr(intake, "board_to_servicing", _explode)
    body = {
        "app_id": 1,
        "applicant_name": "Anon",
        "principal": 15000,
        "annual_rate_pct": 7.99,
        "term_months": 48,
    }
    client = TestClient(app, raise_server_exceptions=False)
    assert client.post("/board", json=body).status_code == 403
    assert (
        client.post(
            "/board", json=body, headers={"X-Internal-Service": "wrong"}
        ).status_code
        == 403
    )
