"""ADR 0010 officer-OR-owner authorization (PR review).

The /los proxy reaches origination anonymously, so the application-scoped routes must
authorize the caller themselves: an officer (underwriter/admin) may act on any
application, the owning borrower may act only on their own, and everyone else --
including an anonymous caller with no X-User-Id -- is denied. A non-owner is denied as
404, never 403-on-exists, so a caller cannot enumerate which application ids are real.
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import authz
from app.database import get_session
from app.main import app
from app.routers import applications


def _authz_db(user_row, app_applicant_id, app_token=None, app_exists=None):
    """Stub authz.db.query: the users lookup returns user_row ([] = no such user); the
    applications lookup returns the app's applicant_id + continuation_token ([] = no such
    application). app_exists defaults to "the app row is present iff it has an applicant_id
    or a token"; pass it explicitly to model an owner-less, token-less row that still
    exists."""

    exists = app_exists
    if exists is None:
        exists = app_applicant_id is not None or app_token is not None

    def _q(sql, params=None):
        if "FROM users" in sql:
            return [user_row] if user_row is not None else []
        if "FROM applications" in sql:
            return (
                [{"applicant_id": app_applicant_id, "continuation_token": app_token}]
                if exists
                else []
            )
        raise AssertionError(f"unexpected query: {sql}")

    return _q


# --- require_officer_or_owner -------------------------------------------------


@pytest.mark.parametrize("role", ["underwriter", "admin", "Underwriter", " ADMIN "])
def test_officer_allowed_without_touching_db(monkeypatch, role):
    # An officer short-circuits before any DB lookup -- so the DB stub must never run.
    def _boom(*a, **k):
        raise AssertionError("officer path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    authz.require_officer_or_owner(1, role, None)  # no raise


def test_owner_allowed(monkeypatch):
    # Borrower user 5 -> applicant 1; application 1 is owned by applicant 1 -> allowed.
    monkeypatch.setattr(
        authz.db, "query", _authz_db({"applicant_id": 1}, app_applicant_id=1)
    )
    authz.require_officer_or_owner(1, "borrower", "5")  # no raise


def test_non_owner_borrower_denied_404(monkeypatch):
    # Borrower user 5 -> applicant 1, but application 1 belongs to applicant 2.
    monkeypatch.setattr(
        authz.db, "query", _authz_db({"applicant_id": 1}, app_applicant_id=2)
    )
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, "borrower", "5")
    assert exc.value.status_code == 404  # no existence oracle: not 403


def test_anonymous_denied_without_db(monkeypatch):
    # No X-User-Id (anonymous /los caller): denied before any DB lookup.
    def _boom(*a, **k):
        raise AssertionError("anonymous path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, None, None)
    assert exc.value.status_code == 404


def test_unknown_user_denied(monkeypatch):
    # X-User-Id present but no such user row -> denied.
    monkeypatch.setattr(authz.db, "query", _authz_db(None, app_applicant_id=1))
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, "borrower", "999")
    assert exc.value.status_code == 404


def test_non_numeric_user_id_denied_without_db(monkeypatch):
    # A non-numeric X-User-Id can never match users.id (integer) -> fail closed, no query.
    def _boom(*a, **k):
        raise AssertionError("bad user id must not reach the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, "borrower", "not-a-number")
    assert exc.value.status_code == 404


def test_null_owner_application_denied_for_borrower(monkeypatch):
    # An anonymously-created application (applicant_id present but no owning user) cannot
    # be owned by any borrower -- so a borrower with a real applicant_id that happens not
    # to match is denied. Here the app's applicant_id is NULL (unlinked).
    monkeypatch.setattr(
        authz.db, "query", _authz_db({"applicant_id": 7}, app_applicant_id=None)
    )
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, "borrower", "5")
    assert exc.value.status_code == 404


def test_officer_user_with_null_applicant_still_allowed(monkeypatch):
    # Officers carry users.applicant_id = NULL; the role check must win regardless.
    def _boom(*a, **k):
        raise AssertionError("officer path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    authz.require_officer_or_owner(1, "admin", "1")  # no raise


# --- continuation token (ADR 0010 Phase B: anonymous applicant, no login) -----


def test_valid_continuation_token_allowed(monkeypatch):
    # Anonymous applicant (no X-User-Id) holding this application's token is authorized.
    monkeypatch.setattr(
        authz.db, "query", _authz_db(None, app_applicant_id=None, app_token="tok-abc")
    )
    authz.require_officer_or_owner(
        1, None, None, x_application_token="tok-abc"
    )  # no raise


def test_wrong_continuation_token_denied(monkeypatch):
    monkeypatch.setattr(
        authz.db, "query", _authz_db(None, app_applicant_id=None, app_token="tok-real")
    )
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, None, None, x_application_token="tok-wrong")
    assert exc.value.status_code == 404


def test_token_is_scoped_to_its_application(monkeypatch):
    # A token minted for application 1 must not authorize application 2: each application's
    # stored token is compared, so a token-for-1 fails the compare against app 2's token.
    def _q(sql, params=None):
        if "FROM applications" in sql:
            app_id = params[0]
            return [{"applicant_id": None, "continuation_token": f"token-for-{app_id}"}]
        return []

    monkeypatch.setattr(authz.db, "query", _q)
    authz.require_officer_or_owner(1, None, None, x_application_token="token-for-1")
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(2, None, None, x_application_token="token-for-1")
    assert exc.value.status_code == 404


def test_token_against_null_token_row_denied(monkeypatch):
    # An officer-created / legacy row has no continuation_token -> the token path is closed
    # (a caller cannot supply a matching token for a NULL).
    monkeypatch.setattr(
        authz.db,
        "query",
        _authz_db(None, app_applicant_id=9, app_token=None, app_exists=True),
    )
    with pytest.raises(HTTPException) as exc:
        authz.require_officer_or_owner(1, None, None, x_application_token="anything")
    assert exc.value.status_code == 404


def test_owner_still_allowed_with_token_column_present(monkeypatch):
    # Regression: adding the token path must not break the owner path. Owner matches even
    # with a token column present and no token supplied.
    monkeypatch.setattr(
        authz.db, "query", _authz_db({"applicant_id": 1}, app_applicant_id=1)
    )
    authz.require_officer_or_owner(1, "borrower", "5")  # no raise


# --- require_officer (list / assistant) ---------------------------------------


def test_require_officer_allows_officer():
    authz.require_officer("underwriter")  # no raise


@pytest.mark.parametrize("role", ["borrower", "csr", None, ""])
def test_require_officer_denies_non_officer(role):
    with pytest.raises(HTTPException) as exc:
        authz.require_officer(role)
    assert exc.value.status_code == 403


# --- full-stack wiring: anonymous is denied on every application-scoped route -


def test_anonymous_accept_denied_through_route():
    # authz runs first, so an anonymous accept is denied before any boarding DB work.
    resp = TestClient(app).post("/applications/1/accept")
    assert resp.status_code == 404


def test_anonymous_offer_denied_through_route():
    resp = TestClient(app).post("/offer", json={"app_id": 1})
    assert resp.status_code == 404


def test_anonymous_decision_denied_through_route():
    resp = TestClient(app).post("/applications/1/decision")
    assert resp.status_code == 404


def test_anonymous_recheck_kyc_denied_through_route():
    # recheck-kyc re-runs a regulated identity check + persists a kyc_checks row, so it is
    # application-scoped like decision/offer/accept: authz runs first, an anonymous caller
    # is denied 404 before any applicant PII is loaded or KYC is re-run.
    resp = TestClient(app).post("/applications/1/recheck-kyc")
    assert resp.status_code == 404


def test_anonymous_list_denied_through_route():
    # The roster is officer-only (PII dump) -> 403 for an anonymous caller. Override the
    # DB session dependency (resolved before the handler body) so the test exercises the
    # authz gate, not the test env's absent database.
    app.dependency_overrides[get_session] = lambda: None
    try:
        resp = TestClient(app).get("/applications")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 403


# --- end-to-end: the public (logged-out) apply flow works via the token --------


_E2E_TOKEN = "e2e-continuation-token"


def _apply_flow_db(state):
    """Stateful db.query for the public apply flow: the authz lookup returns the token that
    create_application issued (the token is now persisted in the application INSERT, which
    is stubbed, so the lookup returns the known fixed token); decision_request_payload
    returns a decisionable row. Models an owner-less (anonymous) application authorized
    purely by its token."""

    def _q(sql, params=None):
        s = sql.strip().upper()
        if "CONTINUATION_TOKEN FROM APPLICATIONS" in s:  # authz lookup
            return [{"applicant_id": None, "continuation_token": _E2E_TOKEN}]
        if "LEFT JOIN KYC_CHECKS" in s:  # ADR 0011 KYC gate -> passing
            return [{"name_verified": True, "address_verified": True}]
        if s.startswith("SELECT APPLICANT_ID FROM APPLICATIONS"):  # submit resolve
            return [{"applicant_id": None}]
        if "LEFT JOIN APPLICANTS" in s:  # decision_request_payload
            return [
                {
                    "id": 1,
                    "applicant_id": None,
                    "amount": 15000,
                    "term_months": 36,
                    "income": 50000,
                    "monthly_debt": 500,
                    "employment_years": 3,
                    "name": "Jane",
                    "ssn": "123456789",
                }
            ]
        return []

    return _q


def _apply_flow_clients_post(base, path, payload):
    if "/kyc" in path:
        return {"cip_passed": True}
    if "/decision" in path:
        return {"outcome": "approve", "score": 700, "reason": None}
    raise AssertionError(f"unexpected downstream call: {path}")


def test_public_apply_flow_completes_with_continuation_token(monkeypatch):
    # e2e for the logged-out applicant (the reviewer's ask): submit returns a token, and a
    # decision carrying that token as X-Application-Token succeeds -- while the same
    # decision with NO token is denied 404. Proves anonymous apply still works end to end
    # without a login, and only with the scoped capability.
    state = {"token": None}
    monkeypatch.setattr(
        applications.intake, "create_application", lambda payload: (1, _E2E_TOKEN)
    )
    monkeypatch.setattr(applications.db, "query", _apply_flow_db(state))
    monkeypatch.setattr(applications.clients, "post", _apply_flow_clients_post)
    client = TestClient(app)

    submitted = client.post(
        "/applications",
        json={"name": "Jane", "amount": 15000, "monthly_debt": 500},
    )
    assert submitted.status_code == 200
    token = submitted.json()["continuation_token"]
    assert token == _E2E_TOKEN  # issued at submit and returned once

    # No token -> denied (anonymous, no capability).
    denied = client.post("/applications/1/decision")
    assert denied.status_code == 404

    # With the issued token -> authorized, decision returns.
    ok = client.post("/applications/1/decision", headers={"X-Application-Token": token})
    assert ok.status_code == 200
    assert ok.json()["decision"] == "approve"
