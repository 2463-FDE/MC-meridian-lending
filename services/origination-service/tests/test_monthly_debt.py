"""POST /applications/{app_id}/monthly-debt capture-only tests (PR review).

The remediation endpoint is a NULL-row quarantine escape hatch: it may capture a
monthly_debt that was never recorded, but it must NOT overwrite an application whose
monthly_debt is already set. Otherwise a caller who knows an app id could lower an
already-submitted/decisioned application's debt to force a more favorable re-decision.
db.query is stubbed (no live Postgres in unit tests).
"""

import pytest
from fastapi import HTTPException

from app.routers import applications
from app.schemas import MonthlyDebtIn


def _stub_db(monkeypatch, responses):
    """Stub applications.db.query with canned rows per call; record the SQL issued."""
    calls = iter(responses)
    issued = []

    def _query(sql, params=None):
        issued.append((sql, params))
        return next(calls)

    monkeypatch.setattr(applications.db, "query", _query)
    return issued


def test_captures_when_monthly_debt_is_null(monkeypatch):
    # SELECT returns a row with NULL monthly_debt -> UPDATE runs, 200.
    issued = _stub_db(monkeypatch, [[{"monthly_debt": None}], []])
    result = applications.capture_monthly_debt(42, MonthlyDebtIn(monthly_debt=500))
    assert result == {"app_id": 42, "monthly_debt": 500}
    # Two statements: existence/state check then the guarded UPDATE.
    assert len(issued) == 2
    assert "UPDATE applications" in issued[1][0]
    assert "monthly_debt IS NULL" in issued[1][0]  # guard, not an unconditional write


def test_already_recorded_is_409_and_no_update(monkeypatch):
    # SELECT returns an already-populated row -> 409, and the UPDATE never runs.
    issued = _stub_db(monkeypatch, [[{"monthly_debt": 700.0}]])
    with pytest.raises(HTTPException) as exc:
        applications.capture_monthly_debt(42, MonthlyDebtIn(monthly_debt=100))
    assert exc.value.status_code == 409
    assert len(issued) == 1  # rejected before any UPDATE


def test_missing_application_is_404(monkeypatch):
    issued = _stub_db(monkeypatch, [[]])
    with pytest.raises(HTTPException) as exc:
        applications.capture_monthly_debt(999, MonthlyDebtIn(monthly_debt=100))
    assert exc.value.status_code == 404
    assert len(issued) == 1  # no UPDATE attempted for a nonexistent app
