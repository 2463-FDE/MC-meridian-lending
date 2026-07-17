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


_APPROVED_WITH_OFFER = {
    "amount": 5000.0,
    "term_months": 36,
    "name": "Maria",
    "apr": 8.5,
    "outcome": "approve",
}


def _accept_db(joined_row, existing_loan=None):
    """SQL-aware db.query stub for accept_offer:
    - the approval/offer join (FROM applications) returns joined_row (or [] if None);
    - the existing-loan idempotency check (FROM loans) returns [{"id": existing_loan}]
      when existing_loan is not None, else [];
    - the status=funded UPDATE returns []."""

    def _q(sql, params=None):
        s = sql.strip().upper()
        if s.startswith("UPDATE"):
            return []
        if "FROM LOANS" in s:
            return (
                [{"id": existing_loan, "principal": 5000.0}]
                if existing_loan is not None
                else []
            )
        return [joined_row] if joined_row else []

    return _q


def test_accept_boards_when_approved_with_offer(monkeypatch):
    monkeypatch.setattr(applications.db, "query", _accept_db(_APPROVED_WITH_OFFER))
    monkeypatch.setattr(applications.intake, "board_to_servicing", lambda *a, **k: 111)
    resp = TestClient(app).post("/applications/1/accept")
    assert resp.status_code == 200
    assert resp.json()["loan_id"] == 111


def test_accept_is_idempotent_on_retry(monkeypatch):
    # A double-click / timeout-retry after boarding must return the existing loan, not
    # board a second one.
    monkeypatch.setattr(
        applications.db, "query", _accept_db(_APPROVED_WITH_OFFER, existing_loan=555)
    )

    def _must_not_board(*a, **k):
        raise AssertionError("retry must not board a second loan")

    monkeypatch.setattr(applications.intake, "board_to_servicing", _must_not_board)
    resp = TestClient(app).post("/applications/1/accept")
    assert resp.status_code == 200
    assert resp.json()["loan_id"] == 555  # the already-boarded loan, replayed


def test_accept_concurrent_race_replays_winners_loan(monkeypatch):
    # Two concurrent accepts: the pre-check misses for both, the loser's INSERT hits the
    # uq_loans_app UniqueViolation and must replay the winner's loan, never board a second.
    state = {"loans_calls": 0}

    def _q(sql, params=None):
        s = sql.strip().upper()
        if s.startswith("UPDATE"):
            return []
        if "FROM LOANS" in s:
            state["loans_calls"] += 1
            # first check misses (pre-insert); post-conflict lookup finds the winner
            return (
                [] if state["loans_calls"] == 1 else [{"id": 777, "principal": 5000.0}]
            )
        return [_APPROVED_WITH_OFFER]

    monkeypatch.setattr(applications.db, "query", _q)

    def _lost_race(*a, **k):
        raise applications.pg_errors.UniqueViolation("duplicate key uq_loans_app")

    monkeypatch.setattr(applications.intake, "board_to_servicing", _lost_race)
    resp = TestClient(app).post("/applications/1/accept")
    assert resp.status_code == 200
    assert resp.json()["loan_id"] == 777  # the winner's loan, not a second board


def test_accept_replay_reconciles_funded_status_and_balance(monkeypatch):
    # PR review: a first attempt may board the loan then crash before funding the app (or
    # creating the balance). A replay that finds the existing loan must NOT return early —
    # it must still run the (idempotent) balance insert and funded update to self-heal the
    # partial-failure window, not report success while the LOS state stays stale.
    sql_log = []
    balance_params = {}

    def _q(sql, params=None):
        sql_log.append(" ".join(sql.split()))
        s = sql.strip().upper()
        if s.startswith("UPDATE"):
            return []
        if "INSERT INTO BALANCES" in s:
            balance_params["params"] = params
            return []
        if "FROM LOANS" in s:
            # loan boarded by the crashed first attempt; its principal (4800) DIFFERS
            # from the current application amount (5000) to prove the reconcile uses the
            # loan's own principal, not the request/application amount (teeth fix).
            return [{"id": 555, "principal": 4800.0}]
        return [_APPROVED_WITH_OFFER]

    monkeypatch.setattr(applications.db, "query", _q)

    def _no_reboard(*a, **k):
        raise AssertionError("replay must not board again")

    monkeypatch.setattr(applications.intake, "board_to_servicing", _no_reboard)
    resp = TestClient(app).post("/applications/1/accept")
    assert resp.status_code == 200
    assert resp.json()["loan_id"] == 555
    # the reconcile ran on the existing-loan replay path, not skipped
    assert any(
        s.startswith("UPDATE applications SET status = 'funded'") for s in sql_log
    )
    assert any("INSERT INTO balances" in s for s in sql_log)
    # a missing-balance heal uses the BOARDED loan's principal (4800), never the
    # divergent application amount (5000)
    assert balance_params["params"][1] == 4800.0


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
