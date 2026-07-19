"""POST /applications/{app_id}/monthly-debt: internal-only, capture-only tests.

The remediation endpoint is a NULL-row quarantine escape hatch. It must:
  - require the X-Internal-Service secret (the gateway strips it from external
    requests, so only a server-side/ops caller holding the token reaches it);
  - fail closed when the token is unconfigured;
  - capture only when monthly_debt IS NULL, treating a zero-row UPDATE (the
    check-then-write race) as a 409 rather than a false success;
  - write an audit_logs row for every capture.
db.query is stubbed (no live Postgres in unit tests).
"""

import pytest
from fastapi import HTTPException

from app.routers import applications
from app.schemas import MonthlyDebtIn

TOKEN = "test-internal-token"


def _stub_db(monkeypatch, responses):
    """Stub applications.db.query with canned rows per call; record every SQL issued."""
    calls = iter(responses)
    issued = []

    def _query(sql, params=None):
        issued.append((sql, params))
        return next(calls)

    monkeypatch.setattr(applications.db, "query", _query)
    return issued


def _auth(monkeypatch, token=TOKEN):
    monkeypatch.setattr(applications.config, "INTERNAL_SERVICE_TOKEN", token)


def test_captures_when_monthly_debt_is_null(monkeypatch):
    _auth(monkeypatch)
    # SELECT (null) -> UPDATE RETURNING (one row) -> audit INSERT.
    issued = _stub_db(monkeypatch, [[{"monthly_debt": None}], [{"id": 42}], []])
    result = applications.capture_monthly_debt(
        42, MonthlyDebtIn(monthly_debt=500), x_internal_service=TOKEN, x_user_id="u1"
    )
    assert result == {"app_id": 42, "monthly_debt": 500}
    assert len(issued) == 3
    assert "monthly_debt IS NULL" in issued[1][0] and "RETURNING" in issued[1][0]
    assert "INSERT INTO audit_logs" in issued[2][0]
    # Audit actor is the supplied caller identity, action names the operation.
    assert issued[2][1][0] == "u1"
    assert issued[2][1][1] == "capture_monthly_debt"


def test_audit_actor_defaults_to_service_when_no_user(monkeypatch):
    _auth(monkeypatch)
    issued = _stub_db(monkeypatch, [[{"monthly_debt": None}], [{"id": 42}], []])
    # Direct call: pass x_user_id=None explicitly (omitting it would leave the FastAPI
    # Header default object, which only dependency injection resolves).
    applications.capture_monthly_debt(
        42, MonthlyDebtIn(monthly_debt=500), x_internal_service=TOKEN, x_user_id=None
    )
    assert issued[2][1][0] == "internal-service"


def test_zero_row_update_race_is_409_no_false_success(monkeypatch):
    _auth(monkeypatch)
    # SELECT sees NULL, but a concurrent capture lands first: the guarded UPDATE
    # RETURNING comes back empty. Must 409, and must NOT write an audit row.
    issued = _stub_db(monkeypatch, [[{"monthly_debt": None}], []])
    with pytest.raises(HTTPException) as exc:
        applications.capture_monthly_debt(
            42, MonthlyDebtIn(monthly_debt=500), x_internal_service=TOKEN
        )
    assert exc.value.status_code == 409
    assert len(issued) == 2  # no audit INSERT after a lost race


def test_already_recorded_is_409_and_no_update(monkeypatch):
    _auth(monkeypatch)
    issued = _stub_db(monkeypatch, [[{"monthly_debt": 700.0}]])
    with pytest.raises(HTTPException) as exc:
        applications.capture_monthly_debt(
            42, MonthlyDebtIn(monthly_debt=100), x_internal_service=TOKEN
        )
    assert exc.value.status_code == 409
    assert len(issued) == 1  # rejected before any UPDATE


def test_missing_application_is_404(monkeypatch):
    _auth(monkeypatch)
    issued = _stub_db(monkeypatch, [[]])
    with pytest.raises(HTTPException) as exc:
        applications.capture_monthly_debt(
            999, MonthlyDebtIn(monthly_debt=100), x_internal_service=TOKEN
        )
    assert exc.value.status_code == 404
    assert len(issued) == 1


def test_missing_internal_header_is_403_before_any_db(monkeypatch):
    _auth(monkeypatch)
    issued = _stub_db(monkeypatch, [])  # any db call would StopIteration
    with pytest.raises(HTTPException) as exc:
        # x_internal_service=None mimics an absent header (what HTTP DI would pass).
        applications.capture_monthly_debt(
            42, MonthlyDebtIn(monthly_debt=100), x_internal_service=None
        )
    assert exc.value.status_code == 403
    assert issued == []  # gate ran before touching the DB


def test_wrong_internal_header_is_403(monkeypatch):
    _auth(monkeypatch)
    _stub_db(monkeypatch, [])
    with pytest.raises(HTTPException) as exc:
        applications.capture_monthly_debt(
            42, MonthlyDebtIn(monthly_debt=100), x_internal_service="wrong"
        )
    assert exc.value.status_code == 403


def test_unconfigured_token_fails_closed_503(monkeypatch):
    _auth(monkeypatch, token="")  # not configured
    _stub_db(monkeypatch, [])
    with pytest.raises(HTTPException) as exc:
        applications.capture_monthly_debt(
            42, MonthlyDebtIn(monthly_debt=100), x_internal_service=""
        )
    assert exc.value.status_code == 503
