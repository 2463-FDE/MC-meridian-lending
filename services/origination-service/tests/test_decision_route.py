"""POST /applications/{app_id}/decision idempotency tests (PR #7 review).

The officer decision route must forward an Idempotency-Key header to decision-service as
its request_id so a retry after a timeout replays the recorded decision instead of
re-pulling credit and appending a second regulated event. Downstream HTTP is stubbed.
"""

import pytest
from fastapi import HTTPException

from app.routers import applications


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
    applications.run_decision(42, idempotency_key="officer-key-1")
    assert captured_payload["request_id"] == "officer-key-1"


def test_absent_idempotency_key_is_an_explicit_redecision(captured_payload):
    applications.run_decision(42, idempotency_key=None)
    assert "request_id" not in captured_payload  # no key -> no replay, fresh decision


def test_overlong_idempotency_key_rejected_before_downstream(captured_payload):
    with pytest.raises(HTTPException) as exc:
        applications.run_decision(42, idempotency_key="x" * 65)
    assert exc.value.status_code == 400
    assert captured_payload == {}  # rejected before any downstream decision call
