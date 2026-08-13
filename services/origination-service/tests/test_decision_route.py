"""POST /applications/{app_id}/decision idempotency tests (PR #7 review).

The officer decision route must forward an Idempotency-Key header to decision-service as
its request_id so a retry after a timeout replays the recorded decision instead of
re-pulling credit and appending a second regulated event. Downstream HTTP is stubbed.
"""

import httpx
import pytest
from fastapi import HTTPException

from app.routers import applications


@pytest.fixture(autouse=True)
def _kyc_passes(monkeypatch):
    # These tests exercise decision idempotency/error mapping, not the ADR 0011 KYC gate
    # (covered in test_kyc_gate.py). Let KYC pass so its 409 doesn't mask the behavior.
    monkeypatch.setattr(
        applications.kyc_gate, "require_kyc_passed", lambda app_id: None
    )


@pytest.fixture
def captured_payload(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        applications,
        "decision_request_payload",
        lambda app_id: {"application_id": app_id},
    )

    def _post(base, path, payload):
        captured.clear()
        captured.update(payload)
        return {"outcome": "deny", "score": 518, "reason": "x"}

    monkeypatch.setattr(applications.clients, "post", _post)
    return captured


def test_idempotency_key_forwarded_as_request_id(captured_payload):
    applications.run_decision(
        42, idempotency_key="officer-key-1", x_user_role="underwriter"
    )
    assert captured_payload["request_id"] == "officer-key-1"


def test_absent_idempotency_key_is_an_explicit_redecision(captured_payload):
    applications.run_decision(42, idempotency_key=None, x_user_role="underwriter")
    assert "request_id" not in captured_payload  # no key -> no replay, fresh decision


def test_overlong_idempotency_key_rejected_before_downstream(captured_payload):
    with pytest.raises(HTTPException) as exc:
        applications.run_decision(
            42, idempotency_key="x" * 65, x_user_role="underwriter"
        )
    assert exc.value.status_code == 400
    assert captured_payload == {}  # rejected before any downstream decision call


def test_downstream_refusal_maps_to_503_not_500(monkeypatch):
    # PR #7 review: decision-service fails closed with 503 (bureau/record/unmapped
    # feature). run_decision must surface that as a retryable decisioning-unavailable,
    # not let it bubble to FastAPI's global handler as a LOS 500.
    monkeypatch.setattr(
        applications,
        "decision_request_payload",
        lambda app_id: {"application_id": app_id},
    )

    def _post_503(base, path, payload):
        request = httpx.Request("POST", f"{base}{path}")
        response = httpx.Response(503, request=request, json={"detail": "unavailable"})
        raise httpx.HTTPStatusError("503", request=request, response=response)

    monkeypatch.setattr(applications.clients, "post", _post_503)
    with pytest.raises(HTTPException) as exc:
        applications.run_decision(42, idempotency_key=None, x_user_role="underwriter")
    assert exc.value.status_code == 503
    assert exc.value.detail == "decisioning unavailable"


def test_downstream_conflict_maps_to_409_not_503(monkeypatch):
    # A reused idempotency key with changed inputs comes back from decision-service as
    # 409; the LOS must preserve the conflict, not mask it as a retryable 503.
    monkeypatch.setattr(
        applications,
        "decision_request_payload",
        lambda app_id: {"application_id": app_id},
    )

    def _post_409(base, path, payload):
        request = httpx.Request("POST", f"{base}{path}")
        response = httpx.Response(409, request=request, json={"detail": "conflict"})
        raise httpx.HTTPStatusError("409", request=request, response=response)

    monkeypatch.setattr(applications.clients, "post", _post_409)
    with pytest.raises(HTTPException) as exc:
        applications.run_decision(
            42, idempotency_key="reused-key", x_user_role="underwriter"
        )
    assert exc.value.status_code == 409


# --- Reg B principal reasons: plural, not the legacy first-only field -----------------
#
# decision-service ranks up to four specific principal reasons (reasons.MAX_REASONS) and
# returns them in `principal_reasons`; its `reason` field is documented as the FIRST one
# only, kept for legacy callers (decision-service schemas.py:29-33). Forwarding `reason`
# alone means an applicant denied for three reasons is told one of them, while the
# decision_events row an examiner reads carries all three. 12 CFR 1002.9 requires the
# specific principal reason(s), and policies/underwriting_guidelines.md:22-28 says the
# same. The truncation happened in this route, not in decision-service.

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


def _stub_decision(monkeypatch, response):
    monkeypatch.setattr(
        applications,
        "decision_request_payload",
        lambda app_id: {"application_id": app_id},
    )
    monkeypatch.setattr(
        applications.clients, "post", lambda base, path, payload: response
    )


def test_every_principal_reason_reaches_the_response(monkeypatch):
    _stub_decision(
        monkeypatch,
        {
            "outcome": "deny",
            "score": 518,
            "reason": _THREE_REASONS[0]["reason"],
            "principal_reasons": _THREE_REASONS,
        },
    )
    out = applications.run_decision(42, idempotency_key=None, x_user_role="underwriter")
    assert [r.model_dump() for r in out.principal_reasons] == _THREE_REASONS
    # The legacy single-string field keeps its meaning for callers already reading it.
    assert out.adverse_action_reason == _THREE_REASONS[0]["reason"]


def test_absent_principal_reasons_is_an_empty_list_not_none(monkeypatch):
    # An approve carries no reasons, and a decision-service predating the field carries
    # none either. Both must give the screens an empty list to iterate, never null.
    _stub_decision(monkeypatch, {"outcome": "approve", "score": 712})
    out = applications.run_decision(42, idempotency_key=None, x_user_role="underwriter")
    assert out.principal_reasons == []
    assert out.adverse_action_reason is None


# --- Malformed downstream principal_reasons must not reach the response verbatim -----
#
# decision-service can rebuild principal_reasons from the persisted decision_events row
# on idempotency replay (unconstrained JSONB, decision.py:198), and GET application
# detail already allowlists that same column to {code, reason, feature}. This route must
# sanitize identically instead of forwarding resp["principal_reasons"] verbatim, or a
# malformed/backfilled/hand-edited event leaks extra internal keys or non-dict items to
# a borrower or officer calling decisioning (PR 34 review).


def test_malformed_principal_reasons_are_sanitized_not_forwarded(monkeypatch):
    _stub_decision(
        monkeypatch,
        {
            "outcome": "deny",
            "score": 518,
            "reason": "Excessive obligations in relation to income",
            "principal_reasons": [
                {
                    "code": "R02",
                    "reason": "Excessive obligations in relation to income",
                    "feature": "payment_burden",
                    "internal_model_weight": 0.83,  # unknown key must be dropped
                },
                "not-a-reason-object",  # non-dict item must be dropped entirely
                {
                    "code": 7,
                    "reason": None,
                    "feature": "income_sufficiency",
                },  # non-string values, then reasonless -- item dropped entirely
            ],
        },
    )
    out = applications.run_decision(42, idempotency_key=None, x_user_role="underwriter")
    assert len(out.principal_reasons) == 1
    first = out.principal_reasons[0]
    assert first.model_dump() == {
        "code": "R02",
        "reason": "Excessive obligations in relation to income",
        "feature": "payment_burden",
    }
    assert not hasattr(first, "internal_model_weight")


def test_reasonless_first_item_is_dropped_not_forwarded(monkeypatch):
    # Shares _normalize_principal_reasons with get_application, whose legacy
    # adverse_action_reason is derived from principal_reasons[0].reason -- a leading
    # code-only row must not survive into that slot even on the fresh decision path
    # (Codex review, PR 34).
    _stub_decision(
        monkeypatch,
        {
            "outcome": "deny",
            "score": 518,
            "reason": "Income insufficient for amount of credit requested",
            "principal_reasons": [
                {"code": "R02"},  # no reason text
                {
                    "code": "R03",
                    "reason": "Income insufficient for amount of credit requested",
                    "feature": "income_sufficiency",
                },
            ],
        },
    )
    out = applications.run_decision(42, idempotency_key=None, x_user_role="underwriter")
    assert len(out.principal_reasons) == 1
    assert out.principal_reasons[0].code == "R03"


# --- A malformed downstream score must not raise or fabricate a value ----------------
#
# decision-service's score is unvalidated downstream JSON, same risk class as
# ApplicationDetail's drivers.model_score: `int(round(resp.get("score") or 0))` either
# raises on a nonnumeric value or silently reports 0 for an absent/falsy one (PR 34
# review).


def test_nonnumeric_score_degrades_to_none_not_a_500(monkeypatch):
    _stub_decision(
        monkeypatch,
        {"outcome": "deny", "score": "518", "reason": "Excessive obligations"},
    )
    out = applications.run_decision(42, idempotency_key=None, x_user_role="underwriter")
    assert out.score is None


def test_absent_score_is_none_not_a_fabricated_zero(monkeypatch):
    _stub_decision(monkeypatch, {"outcome": "approve"})
    out = applications.run_decision(42, idempotency_key=None, x_user_role="underwriter")
    assert out.score is None


# --- Staff self-decision block (client ask, 2026-08-12 governance §5) ------------------
#
# The route-level wiring of authz.deny_self_decision (unit-tested in test_authz.py): the
# block runs AFTER require_officer_or_owner authorizes and BEFORE the KYC gate and any
# downstream credit pull, so a blocked attempt never appends a regulated decision event.


def _self_decision_db(caller_applicant_id, app_applicant_id):
    def _q(sql, params=None):
        if "FROM users" in sql:
            return [{"applicant_id": caller_applicant_id}]
        if "FROM applications" in sql:
            return [{"applicant_id": app_applicant_id}]
        raise AssertionError(f"unexpected query: {sql}")

    return _q


def test_officer_cannot_decision_their_own_application(monkeypatch, captured_payload):
    monkeypatch.setattr(applications.authz.db, "query", _self_decision_db(4, 4))
    with pytest.raises(HTTPException) as exc:
        applications.run_decision(
            42, idempotency_key=None, x_user_role="underwriter", x_user_id="9"
        )
    assert exc.value.status_code == 403
    # No credit pull, no decision event: blocked before any downstream call.
    assert captured_payload == {}


def test_self_decision_is_blocked_before_the_kyc_gate(monkeypatch, captured_payload):
    # The block is an authorization decision, not a data-quality one: a passing KYC must
    # not be required to reach it, and a failing one must not mask it.
    def _kyc_boom(app_id):
        raise AssertionError("blocked self-decision must not reach the KYC gate")

    monkeypatch.setattr(applications.kyc_gate, "require_kyc_passed", _kyc_boom)
    monkeypatch.setattr(applications.authz.db, "query", _self_decision_db(4, 4))
    with pytest.raises(HTTPException) as exc:
        applications.run_decision(
            42, idempotency_key=None, x_user_role="underwriter", x_user_id="9"
        )
    assert exc.value.status_code == 403


def test_officer_decisioning_another_applicant_is_unaffected(
    monkeypatch, captured_payload
):
    monkeypatch.setattr(applications.authz.db, "query", _self_decision_db(4, 7))
    out = applications.run_decision(
        42, idempotency_key=None, x_user_role="underwriter", x_user_id="9"
    )
    assert out.decision == "deny"
