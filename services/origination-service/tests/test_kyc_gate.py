"""ADR 0011: a passing CIP/KYC check is required before an application can reach a
regulated / money action (decision, offer, acceptance).

Fails closed: a declined check (CIP false) OR no check at all (kyc-service was down at
submit, so no kyc_checks row) blocks advancement with 409. Pass mirrors kyc-service's own
definition: name_verified AND address_verified.
"""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import kyc_gate
from app.main import app
from app.routers import applications, offers


def _kyc_db(row):
    """Stub for the kyc_gate lookup: returns [row] (the latest kyc_checks join), or [] to
    model a missing application / no kyc_checks row."""

    def _q(sql, params=None):
        assert "LEFT JOIN kyc_checks" in sql
        return [row] if row is not None else []

    return _q


# --- require_kyc_passed unit -------------------------------------------------


def test_passing_kyc_allowed(monkeypatch):
    monkeypatch.setattr(
        kyc_gate.db,
        "query",
        _kyc_db({"name_verified": True, "address_verified": True}),
    )
    kyc_gate.require_kyc_passed(1)  # no raise


@pytest.mark.parametrize(
    "row",
    [
        {"name_verified": False, "address_verified": True},  # name failed
        {"name_verified": True, "address_verified": False},  # address failed
        {"name_verified": False, "address_verified": False},  # both failed
        None,  # no kyc_checks row / missing application (KYC never ran)
    ],
)
def test_failing_or_absent_kyc_blocked(monkeypatch, row):
    monkeypatch.setattr(kyc_gate.db, "query", _kyc_db(row))
    with pytest.raises(HTTPException) as exc:
        kyc_gate.require_kyc_passed(1)
    assert exc.value.status_code == 409


# --- route-level: failed/absent KYC cannot reach decision/offer/accept -------
# authz passes via an officer header (short-circuits before any DB); the KYC gate then
# runs against the stubbed DB. A non-passing result must 409 before any downstream work.

_OFFICER = {"X-User-Role": "underwriter"}


def _kyc_blocks(monkeypatch):
    # latest kyc_checks row present but declined -> gate blocks.
    monkeypatch.setattr(
        applications.db,
        "query",
        _kyc_db({"name_verified": False, "address_verified": False}),
    )


def test_decision_blocked_when_kyc_not_passed(monkeypatch):
    _kyc_blocks(monkeypatch)

    def _must_not_call(*a, **k):
        raise AssertionError("decision-service must not be called on a failed KYC")

    monkeypatch.setattr(applications.clients, "post", _must_not_call)
    resp = TestClient(app).post("/applications/1/decision", headers=_OFFICER)
    assert resp.status_code == 409


def test_offer_blocked_when_kyc_not_passed(monkeypatch):
    _kyc_blocks(monkeypatch)

    def _must_not_post(*a, **k):
        raise AssertionError("disclosure-service must not be called on a failed KYC")

    monkeypatch.setattr(offers.clients, "post", _must_not_post)
    resp = TestClient(app).post("/offer", json={"app_id": 1}, headers=_OFFICER)
    assert resp.status_code == 409


def test_accept_blocked_when_kyc_not_passed(monkeypatch):
    _kyc_blocks(monkeypatch)

    def _must_not_board(*a, **k):
        raise AssertionError("must not board a loan on a failed KYC")

    monkeypatch.setattr(applications.intake, "board_to_servicing", _must_not_board)
    resp = TestClient(app).post("/applications/1/accept", headers=_OFFICER)
    assert resp.status_code == 409


def test_assistant_score_tool_blocked_when_kyc_not_passed(monkeypatch):
    # Parity sweep (playbook officer-vs-assistant twin): the AI assistant's score tool
    # pulls a decision via decision-service just like the manual officer route, so it must
    # honor the same KYC gate -- otherwise "use the assistant" is a KYC bypass.
    from app import assistant

    monkeypatch.setattr(
        assistant, "decision_request_payload", lambda app_id: {"application_id": app_id}
    )
    monkeypatch.setattr(
        assistant.kyc_gate.db,
        "query",
        _kyc_db({"name_verified": False, "address_verified": False}),
    )

    def _must_not_post(*a, **k):
        raise AssertionError("assistant must not pull a decision on a failed KYC")

    monkeypatch.setattr(assistant.clients, "post", _must_not_post)
    with pytest.raises(HTTPException) as exc:
        assistant._score_application(1)
    assert exc.value.status_code == 409


def test_absent_kyc_blocks_decision(monkeypatch):
    # No kyc_checks row (kyc-service was unavailable at submit) -> fail closed, 409.
    monkeypatch.setattr(applications.db, "query", _kyc_db(None))

    def _must_not_call(*a, **k):
        raise AssertionError("decision-service must not be called with no KYC on file")

    monkeypatch.setattr(applications.clients, "post", _must_not_call)
    resp = TestClient(app).post("/applications/1/decision", headers=_OFFICER)
    assert resp.status_code == 409
