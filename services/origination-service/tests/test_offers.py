"""Offer generation + acceptance guards (PR review).

/los/{path} is proxied anonymously, so origination's offer/accept routes must defend
themselves rather than trust the UI flow:
  - money inputs are bound to the stored application, never the caller;
  - a TILA offer is only generated for an APPROVED application;
  - boarding requires an approved decision AND an existing offer (no default-rate board).
The remaining anonymous-trigger authorization (WHOSE application this is) is the separate
officer-OR-owner check deferred to ADR 0010.
"""

from fastapi.testclient import TestClient

from app.main import app
from app.routers import applications, offers


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


def _offer_db(app_row, outcome):
    """SQL-aware db.query stub for make_offer: the applications SELECT returns app_row
    (or [] if None); the decisions SELECT returns the given outcome (or [] if None)."""

    def _q(sql, params=None):
        if "FROM decisions" in sql:
            return [{"outcome": outcome}] if outcome is not None else []
        return [app_row] if app_row else []

    return _q


def test_offer_binds_to_stored_application_ignores_caller_money_fields(monkeypatch):
    capture = {}
    monkeypatch.setattr(
        offers.db, "query", _offer_db({"amount": 5000.0, "term_months": 36}, "approve")
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
    monkeypatch.setattr(offers.db, "query", _offer_db(None, None))

    def _must_not_post(*a, **k):
        raise AssertionError("disclosure-service must not be called for a missing app")

    monkeypatch.setattr(offers.clients, "post", _must_not_post)
    resp = TestClient(app).post("/offer", json={"app_id": 999})
    assert resp.status_code == 404


def test_offer_409_when_not_approved(monkeypatch):
    # Decision-state guard: a denied/referred/undecided application must not get an offer.
    monkeypatch.setattr(
        offers.db, "query", _offer_db({"amount": 5000.0, "term_months": 36}, "deny")
    )

    def _must_not_post(*a, **k):
        raise AssertionError(
            "disclosure-service must not be called for a non-approved app"
        )

    monkeypatch.setattr(offers.clients, "post", _must_not_post)
    resp = TestClient(app).post("/offer", json={"app_id": 1})
    assert resp.status_code == 409


def _accept_db(row):
    """db.query stub for accept_offer: the SELECT join returns `row` (or [] if None); the
    status=funded UPDATE returns []."""

    def _q(sql, params=None):
        if sql.strip().upper().startswith("UPDATE"):
            return []
        return [row] if row else []

    return _q


def test_accept_boards_when_approved_with_offer(monkeypatch):
    monkeypatch.setattr(
        applications.db,
        "query",
        _accept_db(
            {
                "amount": 5000.0,
                "term_months": 36,
                "name": "Maria",
                "apr": 8.5,
                "outcome": "approve",
            }
        ),
    )
    monkeypatch.setattr(applications.intake, "board_to_servicing", lambda *a, **k: 111)
    resp = TestClient(app).post("/applications/1/accept")
    assert resp.status_code == 200
    assert resp.json()["loan_id"] == 111


def test_accept_409_when_not_approved(monkeypatch):
    monkeypatch.setattr(
        applications.db,
        "query",
        _accept_db(
            {
                "amount": 5000.0,
                "term_months": 36,
                "name": "Maria",
                "apr": 8.5,
                "outcome": "deny",
            }
        ),
    )

    def _must_not_board(*a, **k):
        raise AssertionError("must not board a non-approved application")

    monkeypatch.setattr(applications.intake, "board_to_servicing", _must_not_board)
    resp = TestClient(app).post("/applications/1/accept")
    assert resp.status_code == 409


def test_accept_409_when_no_offer(monkeypatch):
    # Approved but no offer generated — must not board at a default rate.
    monkeypatch.setattr(
        applications.db,
        "query",
        _accept_db(
            {
                "amount": 5000.0,
                "term_months": 36,
                "name": "Maria",
                "apr": None,
                "outcome": "approve",
            }
        ),
    )

    def _must_not_board(*a, **k):
        raise AssertionError("must not board without an existing offer")

    monkeypatch.setattr(applications.intake, "board_to_servicing", _must_not_board)
    resp = TestClient(app).post("/applications/1/accept")
    assert resp.status_code == 409
