"""GET /applications/{app_id} carries the latest decision's Reg B reasons (PR review).

`decisions` is outcome-only (models.Decision, debt D4), so before this fix the route
always returned `principal_reasons: []` regardless of what decision_events recorded --
a resumed denied applicant, or an officer opening an existing denial without rerunning
decisioning, saw the status with no reasons. The score/reasons must come from the latest
decision_events row, mirroring disclosure_coordinator.gather_disclosure_context's own
read of that table.
"""

import types

import pytest

from app import models
from app.routers import applications

_THREE_REASONS = [
    {
        "code": "R02",
        "reason": "Excessive obligations in relation to income",
        "feature": "payment_burden",
    },
    {
        "code": "R03",
        "reason": "Income insufficient for amount of credit requested",
        "feature": "income_sufficiency",
    },
    {"code": "R04", "reason": "Length of employment", "feature": "employment_tenure"},
]


class _FakeSession:
    """Duck-typed stand-in for the ORM Session get_application reads.

    applicant_id=None on the application row means the route never issues the KYC
    scalar() query, so scalar() only needs to answer the offer lookup.
    """

    def __init__(self, app_row, decision_row, offer_row=None):
        self._app_row = app_row
        self._decision_row = decision_row
        self._offer_row = offer_row

    def get(self, model, app_id):
        if model is models.Decision:
            return self._decision_row
        return self._app_row

    def scalar(self, stmt):
        return self._offer_row


def _app_row(**overrides):
    base = dict(
        id=1,
        applicant=None,
        applicant_id=None,
        amount=12000.0,
        term_months=48,
        purpose="debt_consolidation",
        status="denied",
        employer=None,
        job_title=None,
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _no_authz(monkeypatch):
    monkeypatch.setattr(
        applications.authz, "require_officer_or_owner", lambda *a, **k: None
    )


def test_detail_carries_every_principal_reason_from_the_latest_event(monkeypatch):
    monkeypatch.setattr(
        applications.db,
        "query",
        lambda sql, params=None: [
            {
                "principal_reasons": _THREE_REASONS,
                "drivers": {"model_score": 518},
            }
        ],
    )
    session = _FakeSession(_app_row(), types.SimpleNamespace(outcome="deny"))

    out = applications.get_application(
        1,
        session=session,
        x_user_role="underwriter",
        x_user_id=None,
        x_application_token=None,
    )

    assert out.decision == "deny"
    assert out.score == 518
    assert out.principal_reasons == _THREE_REASONS
    # Legacy single-string field keeps its meaning for callers already reading it.
    assert out.adverse_action_reason == _THREE_REASONS[0]["reason"]


def test_detail_has_no_reasons_when_no_decision_event_recorded(monkeypatch):
    monkeypatch.setattr(applications.db, "query", lambda sql, params=None: [])
    session = _FakeSession(_app_row(status="submitted"), None)

    out = applications.get_application(
        1,
        session=session,
        x_user_role="underwriter",
        x_user_id=None,
        x_application_token=None,
    )

    assert out.decision is None
    assert out.score is None
    assert out.principal_reasons == []
    assert out.adverse_action_reason is None


# --- malformed decision_events.principal_reasons must not 500 the whole detail view ----
#
# decision_events.principal_reasons is unconstrained JSONB. The live write path
# (decision-service reasons.py) always emits {code, reason, feature}, but a legacy row,
# a hand-edit, or a future backfill is not guaranteed to (teeth review). The route must
# degrade to no legacy reason string, not crash and take the whole application detail
# view down with it.


def test_reason_item_missing_reason_key_does_not_500(monkeypatch):
    monkeypatch.setattr(
        applications.db,
        "query",
        lambda sql, params=None: [
            {
                "principal_reasons": [{"code": "R02"}],
                "drivers": {"model_score": 518},
            }
        ],
    )
    session = _FakeSession(_app_row(), types.SimpleNamespace(outcome="deny"))

    out = applications.get_application(
        1,
        session=session,
        x_user_role="underwriter",
        x_user_id=None,
        x_application_token=None,
    )

    assert out.decision == "deny"
    assert out.principal_reasons == [{"code": "R02"}]
    assert out.adverse_action_reason is None


def test_reason_item_not_a_dict_does_not_500(monkeypatch):
    monkeypatch.setattr(
        applications.db,
        "query",
        lambda sql, params=None: [{"principal_reasons": ["not-a-dict"], "drivers": {}}],
    )
    session = _FakeSession(_app_row(), types.SimpleNamespace(outcome="deny"))

    out = applications.get_application(
        1,
        session=session,
        x_user_role="underwriter",
        x_user_id=None,
        x_application_token=None,
    )

    assert out.adverse_action_reason is None
