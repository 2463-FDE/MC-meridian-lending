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


def _authz_db(user_row, app_applicant_id):
    """Stub authz.db.query: the users lookup returns user_row ([] = no such user), the
    applications lookup returns the app's applicant_id ([] = no such application)."""

    def _q(sql, params=None):
        if "FROM users" in sql:
            return [user_row] if user_row is not None else []
        if "FROM applications" in sql:
            return (
                [{"applicant_id": app_applicant_id}]
                if app_applicant_id is not None
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
