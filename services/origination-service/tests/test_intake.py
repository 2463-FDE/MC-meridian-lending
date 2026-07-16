"""Intake persistence of model inputs (PR #7 review).

The LOS must persist the real underwriting inputs (income, monthly_debt,
employment_years) and forward them to decision-service — not fabricate zeros. DB is
stubbed (no live Postgres in unit tests; the column + end-to-end write are exercised by
the smoke test against the compose stack).
"""

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


def test_decision_request_payload_falls_back_for_legacy_rows(monkeypatch):
    # Rows predating the monthly_debt column (NULL) fall back to 0, not a crash, and
    # DecisionIn's required monthly_debt still receives a number.
    row = {
        "applicant_id": 9,
        "amount": 15000,
        "term_months": 36,
        "income": None,
        "monthly_debt": None,
        "employment_years": None,
        "name": "Legacy",
        "ssn": "123456789",
    }
    monkeypatch.setattr(applications.db, "query", lambda sql, params=None: [row])
    payload = applications.decision_request_payload(1)
    assert payload["monthly_debt"] == 0
    assert payload["employment_years"] == 0
    assert payload["annual_income"] == 0


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
