"""ADR 0014 Decision 1 authorization on payment-service's own POST /payments.

servicing-service's /payments already gated on money-role-or-owner; this route --
the one the gateway and frontend actually call -- did not, so any authenticated
caller could capture a real charge against any loan id and ride the internal-
service token past servicing's gate as a confused deputy (Codex review, PR 32).
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import authz, config
from app.main import app


# --- require_money_role_or_owner -------------------------------------------------


@pytest.mark.parametrize("role", ["csr", "admin", "Csr", " ADMIN "])
def test_money_role_allowed_without_db(monkeypatch, role):
    def _boom(*a, **k):
        raise AssertionError("money-role path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    authz.require_money_role_or_owner(1, role, None)  # no raise


def test_owner_allowed(monkeypatch):
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": 7}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)
    authz.require_money_role_or_owner(1, "borrower", "5")  # no raise


def test_non_owner_borrower_denied_404(monkeypatch):
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": 9}]  # different applicant
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)
    with pytest.raises(HTTPException) as exc:
        authz.require_money_role_or_owner(1, "borrower", "5")
    assert exc.value.status_code == 404


def test_underwriter_denied_on_arbitrary_loan(monkeypatch):
    # An underwriter is staff-shaped but not a money role: falls through to the
    # ownership check, and a staff login carries no applicant_id.
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": None}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)
    with pytest.raises(HTTPException) as exc:
        authz.require_money_role_or_owner(1, "underwriter", "12")
    assert exc.value.status_code == 404


def test_anonymous_denied_without_db(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("anonymous path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    with pytest.raises(HTTPException) as exc:
        authz.require_money_role_or_owner(1, None, None)
    assert exc.value.status_code == 404


# --- full-stack wiring: POST /payments enforces the gate --------------------------


def test_post_payment_denied_for_non_owner_borrower(monkeypatch):
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": 9}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)

    def _boom(*a, **k):
        raise AssertionError("a denied caller must never reach charge()")

    monkeypatch.setattr("app.payments.charge", _boom)
    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 500.0},
        headers={"X-User-Role": "borrower", "X-User-Id": "5"},
    )
    assert resp.status_code == 404


def test_post_payment_denied_for_underwriter_on_arbitrary_loan(monkeypatch):
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": None}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)

    def _boom(*a, **k):
        raise AssertionError("a denied caller must never reach charge()")

    monkeypatch.setattr("app.payments.charge", _boom)
    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 500.0},
        headers={"X-User-Role": "underwriter", "X-User-Id": "12"},
    )
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
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(
        "app.payments.charge",
        lambda loan_id, pan, cvv, amount, ssn, name, method: {
            "payment_id": 1,
            "loan_id": loan_id,
            "status": "captured",
            "applied_amount": amount,
        },
    )
    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 50.0},
        headers={"X-User-Role": "borrower", "X-User-Id": "5"},
    )
    assert resp.status_code == 200


def test_post_payment_allowed_for_money_role_without_db(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("money-role path must not query the database")

    monkeypatch.setattr(authz.db, "query", _boom)
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "sekret")
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(
        "app.payments.charge",
        lambda loan_id, pan, cvv, amount, ssn, name, method: {
            "payment_id": 1,
            "loan_id": loan_id,
            "status": "captured",
            "applied_amount": amount,
        },
    )
    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 50.0},
        headers={"X-User-Role": "csr"},
    )
    assert resp.status_code == 200


# --- captured_unapplied is not a plain 200, and not a retryable 5xx ----------------
#
# The frontend discards the /payments response body and shows a flat "submitted"
# message on any call that does not throw; with no idempotency key (D2/D19), a
# caller who read status="captured_unapplied" as a normal 200 could retry and
# double-charge the card. A 502/503/504 has the same problem from the other
# direction: generic HTTP clients, gateways, and retry libraries treat those as
# "transient, safe to retry" regardless of the detail text, and a retry on this
# exact request would also double-charge. 424 Failed Dependency says the capture
# itself succeeded and failed only because a dependency (servicing's apply call)
# did, without the retry connotation.


def test_post_payment_424_when_captured_unapplied(monkeypatch):
    def _q(sql, params=None):
        if "FROM loans l JOIN applications app" in sql:
            return [{"applicant_id": 7}]
        if "FROM users" in sql:
            return [{"applicant_id": 7}]
        raise AssertionError(f"unexpected query: {sql}")

    monkeypatch.setattr(authz.db, "query", _q)
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "sekret")
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(
        "app.payments.charge",
        lambda loan_id, pan, cvv, amount, ssn, name, method: {
            "payment_id": 42,
            "loan_id": loan_id,
            "status": "captured_unapplied",
            "applied_amount": 0.0,
        },
    )
    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 50.0},
        headers={"X-User-Role": "borrower", "X-User-Id": "5"},
    )
    assert resp.status_code == 424
    assert resp.status_code not in (502, 503, 504), (
        "must not use a status code generic retry logic treats as transient"
    )
    body = resp.json()
    assert "42" in body["detail"]
    assert "not retry" in body["detail"].lower()
