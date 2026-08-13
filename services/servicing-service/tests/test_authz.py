"""ADR 0014 Decision 1 authorization (closes debt D8(b)).

Before this, `adjust_balance`/`waive_fee` declared X-User-Role and never read it, and
every loan-scoped read was reachable by walking serial loan ids -- any authenticated
caller, including a borrower login (confirmed on the public internet), could move
money on any account. Money routes require a servicing money role (CSR/admin);
internal-only routes require the shared X-Internal-Service secret; loan-scoped reads
admit staff or the owning borrower, denied as 404 so a serial id cannot be probed for
existence.
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import authz, config
from app.main import app


# --- require_money_role --------------------------------------------------------


@pytest.mark.parametrize("role", ["csr", "admin", "Csr", " ADMIN "])
def test_money_role_allowed(role):
    authz.require_money_role(role)  # no raise


@pytest.mark.parametrize("role", ["underwriter", "borrower", None, ""])
def test_money_role_denied(role):
    with pytest.raises(HTTPException) as exc:
        authz.require_money_role(role)
    assert exc.value.status_code == 403


# --- require_internal_caller ----------------------------------------------------


def test_internal_caller_allowed_with_matching_token(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    authz.require_internal_caller("sekret")  # no raise


def test_internal_caller_denied_with_wrong_token(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    with pytest.raises(HTTPException) as exc:
        authz.require_internal_caller("wrong")
    assert exc.value.status_code == 403


def test_internal_caller_denied_with_no_token(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    with pytest.raises(HTTPException) as exc:
        authz.require_internal_caller(None)
    assert exc.value.status_code == 403


def test_internal_caller_fails_closed_when_unconfigured(monkeypatch):
    # Unconfigured secret must never fall open -- even a caller presenting the
    # empty string must be refused, and refused as 503 (config problem), not 403.
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "")
    with pytest.raises(HTTPException) as exc:
        authz.require_internal_caller("anything")
    assert exc.value.status_code == 503


def test_internal_caller_denied_on_non_ascii_token_not_500(monkeypatch):
    # hmac.compare_digest raises TypeError comparing a non-ASCII str against bytes
    # unless both sides are encoded first -- must deny 403, never 500.
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    with pytest.raises(HTTPException) as exc:
        authz.require_internal_caller("tökén-🔑")
    assert exc.value.status_code == 403


# --- require_staff_or_owner (loan-scoped reads) ---------------------------------


@pytest.mark.parametrize("role", ["csr", "underwriter", "admin", "Csr", " ADMIN "])
def test_staff_role_allowed_without_touching_db(monkeypatch, role):
    def _boom(*a, **k):
        raise AssertionError("staff path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    authz.require_staff_or_owner(1, role, None)  # no raise


def test_owner_allowed(monkeypatch):
    # Loan 1's application belongs to applicant 7; borrower user 5 is applicant 7.
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": 7}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)
    authz.require_staff_or_owner(1, "borrower", "5")  # no raise


def test_non_owner_borrower_denied_404(monkeypatch):
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": 9}]  # different applicant
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)
    with pytest.raises(HTTPException) as exc:
        authz.require_staff_or_owner(1, "borrower", "5")
    assert exc.value.status_code == 404  # no existence oracle: not 403


def test_anonymous_denied_without_db(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("anonymous path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    with pytest.raises(HTTPException) as exc:
        authz.require_staff_or_owner(1, None, None)
    assert exc.value.status_code == 404


def test_non_numeric_user_id_denied_without_db(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("bad user id must not reach the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    with pytest.raises(HTTPException) as exc:
        authz.require_staff_or_owner(1, "borrower", "not-a-number")
    assert exc.value.status_code == 404


def test_null_app_id_loan_denied_for_borrower(monkeypatch):
    # A legacy loan with no app_id has no derivable owner -- staff-only.
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return []  # the JOIN finds nothing when app_id is NULL
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)
    with pytest.raises(HTTPException) as exc:
        authz.require_staff_or_owner(1, "borrower", "5")
    assert exc.value.status_code == 404


def test_staff_user_with_null_applicant_still_allowed(monkeypatch):
    # Staff logins carry users.applicant_id = NULL; the role check must win regardless.
    def _boom(*a, **k):
        raise AssertionError("staff path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    authz.require_staff_or_owner(1, "admin", "1")  # no raise


# --- require_staff (portfolio-wide routes) --------------------------------------


@pytest.mark.parametrize("role", ["csr", "underwriter", "admin"])
def test_require_staff_allows_staff(role):
    authz.require_staff(role)  # no raise


@pytest.mark.parametrize("role", ["borrower", None, ""])
def test_require_staff_denies_non_staff(role):
    with pytest.raises(HTTPException) as exc:
        authz.require_staff(role)
    assert exc.value.status_code == 403


# --- full-stack wiring: each route enforces its gate ----------------------------


def test_adjust_balance_denied_without_money_role():
    resp = TestClient(app).post(
        "/accounts/1/adjust-balance",
        json={"new_balance": 100.0},
        headers={"X-User-Role": "underwriter"},
    )
    assert resp.status_code == 403


def test_adjust_balance_allowed_with_money_role(monkeypatch):
    monkeypatch.setattr(
        "app.balance.adjust_balance", lambda loan_id, new_value: new_value
    )
    resp = TestClient(app).post(
        "/accounts/1/adjust-balance",
        json={"new_balance": 100.0},
        headers={"X-User-Role": "csr"},
    )
    assert resp.status_code == 200


def test_waive_fee_denied_without_money_role():
    resp = TestClient(app).post(
        "/accounts/1/waive-fee",
        json={"amount": 10.0},
        headers={"X-User-Role": "borrower"},
    )
    assert resp.status_code == 403


def test_apply_payment_denied_without_internal_token(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    resp = TestClient(app).post(
        "/accounts/1/apply-payment", json={"amount": 50.0, "payment_id": 1}
    )
    assert resp.status_code == 403


def test_apply_payment_allowed_with_internal_token(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr("app.balance.apply_payment", lambda loan_id, amount: 50.0)
    resp = TestClient(app).post(
        "/accounts/1/apply-payment",
        json={"amount": 50.0, "payment_id": 1},
        headers={"X-Internal-Service": "sekret"},
    )
    assert resp.status_code == 200


def test_late_fee_denied_without_internal_token(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    resp = TestClient(app).post("/accounts/1/late-fee")
    assert resp.status_code == 403


def test_reconciliation_peek_denied_without_internal_token(monkeypatch):
    """`GET /reconciliation/peek` returns portfolio-wide figures, not one caller's row.

    The borrower portal sits behind the same gateway as the internal app, so a borrower
    login is on the public internet and reads their own account only. This route reports
    across every loan, and it was reachable with no gate at all.
    """
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    resp = TestClient(app).get("/reconciliation/peek")
    assert resp.status_code == 403


def test_reconciliation_peek_denied_with_wrong_internal_token(monkeypatch):
    """Presence of the header is not the check — the value is compared."""
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    resp = TestClient(app).get(
        "/reconciliation/peek", headers={"X-Internal-Service": "not-the-token"}
    )
    assert resp.status_code == 403


def test_reconciliation_peek_allowed_with_internal_token(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr("app.reconciliation.ledger_total", lambda: 0.0)
    monkeypatch.setattr("app.reconciliation.settlement_total", lambda: 0.0)
    resp = TestClient(app).get(
        "/reconciliation/peek", headers={"X-Internal-Service": "sekret"}
    )
    assert resp.status_code == 200


def test_get_balance_denied_for_non_owner_borrower(monkeypatch):
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": 9}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)
    resp = TestClient(app).get(
        "/accounts/1/balance", headers={"X-User-Role": "borrower", "X-User-Id": "5"}
    )
    assert resp.status_code == 404


def test_get_balance_allowed_for_staff(monkeypatch):
    monkeypatch.setattr("app.balance.get_balance", lambda loan_id: 100.0)
    monkeypatch.setattr("app.balance.get_past_due", lambda loan_id: 0.0)
    resp = TestClient(app).get("/accounts/1/balance", headers={"X-User-Role": "csr"})
    assert resp.status_code == 200


# --- /loans router: staff-only list, staff-or-owner reads (ADR 0014 Decision 1) ---
#
# `Depends(get_session)` resolves before the route body runs, so even the denial
# path would otherwise hit the real (unconfigured-in-tests) DATABASE_URL. Every
# /loans test below takes `fake_db_session` to override it with a fake session --
# authz still runs first in the handler body, so the denial tests never touch the
# fake session's methods.


class _FakeResult:
    def all(self):
        return []


class _FakeSession:
    def scalar(self, stmt):
        return 0

    def execute(self, stmt):
        return _FakeResult()

    def get(self, model, ident):
        return None


@pytest.fixture
def fake_db_session():
    from app.database import get_session

    def _override():
        yield _FakeSession()

    app.dependency_overrides[get_session] = _override
    yield
    app.dependency_overrides.clear()


def test_list_loans_denied_for_borrower(fake_db_session):
    resp = TestClient(app).get("/loans", headers={"X-User-Role": "borrower"})
    assert resp.status_code == 403


def test_list_loans_denied_anonymous(fake_db_session):
    resp = TestClient(app).get("/loans")
    assert resp.status_code == 403


def test_list_loans_allowed_for_staff(fake_db_session):
    resp = TestClient(app).get("/loans", headers={"X-User-Role": "csr"})
    assert resp.status_code == 200


def test_get_loan_denied_for_non_owner_borrower(monkeypatch, fake_db_session):
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": 9}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)
    resp = TestClient(app).get(
        "/loans/1", headers={"X-User-Role": "borrower", "X-User-Id": "5"}
    )
    assert resp.status_code == 404


def test_get_loan_denied_anonymous(monkeypatch, fake_db_session):
    def _boom(*a, **k):
        raise AssertionError("anonymous path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    resp = TestClient(app).get("/loans/1")
    assert resp.status_code == 404


def test_loan_schedule_denied_for_non_owner_borrower(monkeypatch, fake_db_session):
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": 9}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)
    resp = TestClient(app).get(
        "/loans/1/schedule", headers={"X-User-Role": "borrower", "X-User-Id": "5"}
    )
    assert resp.status_code == 404


def test_loan_schedule_denied_anonymous(monkeypatch, fake_db_session):
    def _boom(*a, **k):
        raise AssertionError("anonymous path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    resp = TestClient(app).get("/loans/1/schedule")
    assert resp.status_code == 404


def test_loan_payments_denied_for_non_owner_borrower(monkeypatch, fake_db_session):
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": 9}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)
    resp = TestClient(app).get(
        "/loans/1/payments", headers={"X-User-Role": "borrower", "X-User-Id": "5"}
    )
    assert resp.status_code == 404


def test_loan_payments_denied_anonymous(monkeypatch, fake_db_session):
    def _boom(*a, **k):
        raise AssertionError("anonymous path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    resp = TestClient(app).get("/loans/1/payments")
    assert resp.status_code == 404


# --- POST /payments (direct charge route): staff-or-owner (ADR 0014 Decision 1) ---


def test_post_payment_denied_for_non_owner_borrower(monkeypatch):
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": 9}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)

    def _boom(*a, **k):
        raise AssertionError("denied caller must never reach charge()")

    monkeypatch.setattr("app.payments.charge", _boom)
    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 50.0},
        headers={"X-User-Role": "borrower", "X-User-Id": "5"},
    )
    assert resp.status_code == 404


def test_post_payment_denied_anonymous(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("anonymous caller must never reach charge()")

    monkeypatch.setattr("app.payments.charge", _boom)
    resp = TestClient(app).post("/payments", json={"loan_id": 1, "amount": 50.0})
    assert resp.status_code == 404


def test_post_payment_allowed_for_owner(monkeypatch):
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": 7}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "sekret")
    monkeypatch.setattr(
        "app.payments.charge",
        lambda loan_id, pan, cvv, amount, ssn, name, method: {
            "loan_id": loan_id,
            "amount": amount,
            "balance": 0.0,
        },
    )
    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 50.0},
        headers={"X-User-Role": "borrower", "X-User-Id": "5"},
    )
    assert resp.status_code == 200


def test_post_payment_denied_for_underwriter_on_arbitrary_loan(monkeypatch):
    # An underwriter is staff but NOT a money role (_MONEY_ROLES = {csr, admin}):
    # charging a card is a money-moving write like adjust-balance/waive-fee, so a
    # blanket staff pass here would let an underwriter charge any borrower's loan
    # with no ownership check -- the same class of gap review comment 2 closed for
    # borrowers. A staff login carries no applicant_id, so the ownership fallback
    # denies it.
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": None}]  # staff login: no applicant_id
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)

    def _boom(*a, **k):
        raise AssertionError("denied caller must never reach charge()")

    monkeypatch.setattr("app.payments.charge", _boom)
    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 500.0},
        headers={"X-User-Role": "underwriter", "X-User-Id": "12"},
    )
    assert resp.status_code == 404


def test_post_payment_allowed_for_money_role_without_db(monkeypatch):
    # CSR/admin short-circuit like adjust-balance/waive-fee -- no DB read needed
    # and no ownership tie to a specific loan.
    def _boom(*a, **k):
        raise AssertionError("money-role path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "sekret")
    monkeypatch.setattr(
        "app.payments.charge",
        lambda loan_id, pan, cvv, amount, ssn, name, method: {
            "loan_id": loan_id,
            "amount": amount,
            "balance": 0.0,
        },
    )
    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 50.0},
        headers={"X-User-Role": "csr"},
    )
    assert resp.status_code == 200


# --- GET /loans/mine (borrower self-service lookup) -----------------------------
#
# The frontend's "My Loan" page had no way to find a borrower's own loan id except
# the portfolio list, client-side filtered -- which meant any borrower could read
# every OTHER borrower's loans and balances too, before require_staff closed
# GET /loans to staff only. This route replaces that: scoped server-side to the
# caller's own applicant_id, mirroring authz._owns_loan's join.


def test_list_my_loans_returns_only_the_owners_rows(monkeypatch):
    from app import db

    def _q(sql, params=None):
        assert params == (5,)
        assert "WHERE u.id = %s" in sql
        return [
            {
                "id": 4471,
                "applicant_name": "Maria Gonzalez",
                "principal": 18000.0,
                "apr": 7.14,
                "term_months": 48,
                "status": "current",
                "opened_at": None,
                "balance": 12200.0,
                "past_due": 0.0,
            }
        ]

    monkeypatch.setattr(db, "query", _q)
    resp = TestClient(app).get("/loans/mine", headers={"X-User-Id": "5"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == 4471
    assert body["items"][0]["balance"] == 12200.0


def test_list_my_loans_empty_without_user_id(monkeypatch):
    from app import db

    def _boom(*a, **k):
        raise AssertionError("no X-User-Id must never reach the database")

    monkeypatch.setattr(db, "query", _boom)
    resp = TestClient(app).get("/loans/mine")
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "total": 0, "limit": 200, "offset": 0}


def test_list_my_loans_empty_for_non_numeric_user_id(monkeypatch):
    from app import db

    def _boom(*a, **k):
        raise AssertionError("a non-numeric id must never reach the database")

    monkeypatch.setattr(db, "query", _boom)
    resp = TestClient(app).get("/loans/mine", headers={"X-User-Id": "not-a-number"})
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_mine_route_wins_over_loan_id_path_param(monkeypatch):
    # /mine is registered before /{loan_id}; a request for the literal "mine"
    # segment must hit this route, not 422 on int-parsing "mine" as a loan id.
    from app import db

    monkeypatch.setattr(db, "query", lambda sql, params=None: [])
    resp = TestClient(app).get("/loans/mine", headers={"X-User-Id": "5"})
    assert resp.status_code == 200
