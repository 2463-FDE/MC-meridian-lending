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
                },  # non-string values dropped
            ],
        },
    )
    out = applications.run_decision(42, idempotency_key=None, x_user_role="underwriter")
    assert len(out.principal_reasons) == 2
    first = out.principal_reasons[0]
    assert first.model_dump() == {
        "code": "R02",
        "reason": "Excessive obligations in relation to income",
        "feature": "payment_burden",
    }
    assert not hasattr(first, "internal_model_weight")
    second = out.principal_reasons[1]
    assert second.model_dump() == {
        "code": None,
        "reason": None,
        "feature": "income_sufficiency",
    }
