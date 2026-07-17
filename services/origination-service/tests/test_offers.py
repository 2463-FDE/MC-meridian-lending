"""Offer route binds disclosure inputs to the stored application (PR review).

/los/offer is reachable anonymously through the gateway and origination forwards the
internal-service token to disclosure-service, so caller-supplied principal/rate/term
must be ignored in favor of the stored loan terms — else an external caller forges a
persisted TILA offer (later read by accept_offer to board a loan) for any guessed app
id. The remaining anonymous-trigger IDOR is a separate authorization concern (ADR 0010).
"""

from fastapi.testclient import TestClient

from app.main import app
from app.routers import offers


def _disclosure_resp():
    return {
        "disclosure": {
            "apr": 8.5,
            "finance_charge": 100.0,
            "monthly_payment": 200.0,
            "amount_financed": 5000.0,
            "total_of_payments": 5100.0,
        },
        "schedule": [],
    }


def test_offer_binds_to_stored_application_ignores_caller_money_fields(monkeypatch):
    capture = {}
    monkeypatch.setattr(
        offers.db,
        "query",
        lambda sql, params=None: [{"amount": 5000.0, "term_months": 36}],
    )

    def _post(base, path, payload):
        capture["payload"] = payload
        return _disclosure_resp()

    monkeypatch.setattr(offers.clients, "post", _post)
    resp = TestClient(app).post(
        "/offer",
        json={
            "app_id": 1,
            "principal": 50000,  # attacker-inflated — must be ignored
            "annual_rate_pct": 0.01,  # attacker-chosen — must be ignored
            "term_months": 60,  # attacker-chosen — must be ignored
        },
    )
    assert resp.status_code == 200
    fwd = capture["payload"]
    assert fwd["principal"] == 5000.0  # from the stored application, not the caller
    assert fwd["term_months"] == 36  # from the stored application, not the caller
    assert (
        fwd["annual_rate"] == offers.POLICY_RATE_PCT
    )  # server policy, not caller 0.01


def test_offer_404_when_application_missing(monkeypatch):
    monkeypatch.setattr(offers.db, "query", lambda sql, params=None: [])

    def _must_not_post(*a, **k):
        raise AssertionError("disclosure-service must not be called for a missing app")

    monkeypatch.setattr(offers.clients, "post", _must_not_post)
    resp = TestClient(app).post("/offer", json={"app_id": 999})
    assert resp.status_code == 404
