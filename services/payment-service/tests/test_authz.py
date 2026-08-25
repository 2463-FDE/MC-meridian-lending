"""ADR 0014 Decision 1 authorization on payment-service's own POST /payments.

servicing-service's /payments already gated on money-role-or-owner; this route --
the one the gateway and frontend actually call -- did not, so any authenticated
caller could capture a real charge against any loan id and ride the internal-
service token past servicing's gate as a confused deputy (Codex review, PR 32).
"""

import uuid

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import authz, config
from app.main import app


# The charge route now fails closed on an unready schema (D13a: a volume that skipped
# migration 0020 still holds every stored CVV, and the NULLABLE legacy column lets the
# capture insert succeed anyway). These cases have no database at all, so the probe would
# refuse every request and grade nothing but the guard. The guard itself is graded in
# test_no_sad.py (unmigrated volume) and test_db_readiness.py (the rungs).
@pytest.fixture(autouse=True)
def _schema_ready(monkeypatch):
    monkeypatch.setattr(config, "database_reachable", lambda *a, **k: (True, None))


# D19: the Idempotency-Key is required and client-minted (ADR 0013 Decision 1), so every
# call into the charge path has to carry one. A fixed valid UUID keeps these cases
# deterministic; the idempotency behaviour itself is covered by the R-vector suite.
_IDEM_KEY = "11111111-1111-4111-8111-111111111111"


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
        headers={
            "Idempotency-Key": _IDEM_KEY,
            "X-User-Role": "borrower",
            "X-User-Id": "5",
        },
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
        headers={
            "Idempotency-Key": _IDEM_KEY,
            "X-User-Role": "underwriter",
            "X-User-Id": "12",
        },
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
        lambda loan_id, pan, amount, ssn, name, method, request_id=None, **kw: {
            "payment_id": 1,
            "loan_id": loan_id,
            "status": "captured",
            "applied_amount": amount,
        },
    )
    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 50.0},
        headers={
            "Idempotency-Key": _IDEM_KEY,
            "X-User-Role": "borrower",
            "X-User-Id": "5",
        },
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
        lambda loan_id, pan, amount, ssn, name, method, request_id=None, **kw: {
            "payment_id": 1,
            "loan_id": loan_id,
            "status": "captured",
            "applied_amount": amount,
        },
    )
    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 50.0},
        headers={"Idempotency-Key": _IDEM_KEY, "X-User-Role": "csr"},
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
        lambda loan_id, pan, amount, ssn, name, method, request_id=None, **kw: {
            "payment_id": 42,
            "loan_id": loan_id,
            "status": "captured_unapplied",
            "applied_amount": 0.0,
        },
    )
    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 50.0},
        headers={
            "Idempotency-Key": _IDEM_KEY,
            "X-User-Role": "borrower",
            "X-User-Id": "5",
        },
    )
    assert resp.status_code == 424
    assert resp.status_code not in (502, 503, 504), (
        "must not use a status code generic retry logic treats as transient"
    )
    body = resp.json()
    assert "42" in body["detail"]
    assert "not retry" in body["detail"].lower()


def test_post_payment_424_on_replay_of_a_captured_unapplied_row(monkeypatch):
    """B1 (carried): a REPLAY of a captured_unapplied payment must still 424, not
    silently return 200. The REPLAY branch used to return before the
    captured_unapplied check ever ran, so a retry of a 424'd request came back a
    plain success -- and the frontend treats any non-throwing call as submitted."""
    monkeypatch.setattr(authz, "require_money_role_or_owner", lambda *a, **k: None)
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "sekret")
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(
        "app.payments.charge",
        lambda loan_id, pan, amount, ssn, name, method, request_id=None, **kw: {
            "payment_id": 42,
            "loan_id": loan_id,
            "status": "captured_unapplied",
            "applied_amount": 0.0,
            "idempotency": "replay",
        },
    )
    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 50.0},
        headers={"Idempotency-Key": _IDEM_KEY, "X-User-Role": "csr"},
    )
    assert resp.status_code == 424, (
        f"a retry of a captured_unapplied payment must not come back 200, got "
        f"{resp.status_code}: {resp.text}"
    )
    assert resp.headers.get("Idempotent-Replay") == "true"
    body = resp.json()
    assert "42" in body["detail"]


# --- m1: the stored key is the canonical UUID spelling -----------------------------
#
# The route validates the header with uuid.UUID(), which accepts hyphenless and
# uppercase spellings as the same UUID -- but the key is stored as raw TEXT, so two
# spellings of one UUID would claim two distinct rows and dedupe nothing.


def test_hyphenless_and_uppercase_idempotency_keys_canonicalize_the_same(monkeypatch):
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "sekret")
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(authz, "require_money_role_or_owner", lambda *a, **k: None)

    seen = []

    def fake_charge(*a, idempotency_key=None, **kw):
        seen.append(idempotency_key)
        return {
            "payment_id": 1,
            "loan_id": 1,
            "status": "captured",
            "applied_amount": 50.0,
        }

    monkeypatch.setattr("app.payments.charge", fake_charge)

    canonical = uuid.UUID(_IDEM_KEY)
    spellings = [str(canonical), canonical.hex, canonical.hex.upper()]
    for spelling in spellings:
        resp = TestClient(app).post(
            "/payments",
            json={"loan_id": 1, "amount": 50.0},
            headers={"Idempotency-Key": spelling, "X-User-Role": "csr"},
        )
        assert resp.status_code == 200, resp.text

    assert len(set(seen)) == 1, (
        f"every spelling of the same UUID must canonicalize to one stored key: {seen}"
    )
    assert seen[0] == str(canonical)
