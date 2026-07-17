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
