"""The two charge handlers must answer a reused key identically.

ADR 0004 copied the charge handler out of this service into payment-service and left
both routed (debt D23); the client confirmed 2026-08-17 that the second path stays. So
there are two writers of one `payments` table. The partial unique index stops the
double charge for both of them no matter what this code does -- but only if both
handlers actually CLAIM against it, and only if they map the outcomes the same way.
Dedupe in one and the other still 500s on a raw constraint violation, or worse, replays
where its twin refuses.

This is the same failure mode `redactor-drift` exists for, and the same remedy: the
copies are byte-identical and a test says so, rather than a comment asking nicely.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main, payments

REPO = Path(__file__).resolve().parents[3]
BLOCK_START = "# --- D19: idempotency ---"
BLOCK_END = "\n\ndef charge("


def _idempotency_block(service: str) -> str:
    text = (REPO / "services" / service / "app" / "payments.py").read_text()
    return text[
        text.index(BLOCK_START) : text.index(BLOCK_END, text.index(BLOCK_START))
    ]


def test_the_claim_block_is_byte_identical_across_both_handlers():
    mine = _idempotency_block("servicing-service")
    theirs = _idempotency_block("payment-service")
    assert mine == theirs, (
        "the D19 claim block has drifted between servicing-service and "
        "payment-service. Two writers of one payments table must answer a reused key "
        "the same way; resync rather than hand-editing one copy."
    )


def test_the_shipped_claim_sql_spells_the_partial_index_predicate():
    """A bare ON CONFLICT target raises at runtime and disables the control."""
    assert (
        "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING"
        in payments._CLAIM_SQL
    )


def _post(client, **headers):
    return client.post(
        "/payments",
        json={"loan_id": 1, "amount": 250.0, "pan": "4111111111111111", "cvv": "123"},
        headers={"X-User-Role": "csr", **headers},
    )


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(main.config, "PROCESSOR_API_KEY", "proc_test")
    return TestClient(main.app)


def test_a_request_without_a_key_is_refused_before_any_capture(client, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("a keyless request must never reach charge()")

    monkeypatch.setattr(main.payments, "charge", _boom)
    resp = _post(client)
    assert resp.status_code == 400


def test_a_non_uuid_key_is_refused_before_any_capture(client, monkeypatch):
    """The key is the sole arbiter of "same payment".

    A client sending a constant string would collapse genuinely distinct payments into
    one, so refusing is the safe direction.
    """

    def _boom(*a, **k):
        raise AssertionError("a malformed key must never reach charge()")

    monkeypatch.setattr(main.payments, "charge", _boom)
    resp = _post(client, **{"Idempotency-Key": "retry"})
    assert resp.status_code == 400


@pytest.mark.parametrize(
    "outcome,expected",
    [
        (payments.FINGERPRINT_MISMATCH, 422),
        (payments.IN_FLIGHT, 409),
    ],
)
def test_route_maps_each_branch_the_same_way_its_twin_does(
    client, monkeypatch, outcome, expected
):
    monkeypatch.setattr(
        main.payments,
        "charge",
        lambda *a, **k: {
            "loan_id": 1,
            "amount": 0.0,
            "balance": None,
            "payment_id": None,
            "request_id": "x",
            "idempotency": outcome,
        },
    )
    resp = _post(client, **{"Idempotency-Key": "11111111-1111-4111-8111-111111111111"})
    assert resp.status_code == expected
    if expected == 409:
        assert resp.headers.get("Retry-After") == "5"


def test_a_replay_is_flagged_with_the_header_and_returns_the_original(
    client, monkeypatch
):
    monkeypatch.setattr(
        main.payments,
        "charge",
        lambda *a, **k: {
            "loan_id": 1,
            "amount": 250.0,
            "balance": 750.0,
            "payment_id": 7,
            "request_id": "x",
            "idempotency": payments.REPLAY,
        },
    )
    resp = _post(client, **{"Idempotency-Key": "11111111-1111-4111-8111-111111111111"})
    assert resp.status_code == 200
    assert resp.headers.get("Idempotent-Replay") == "true"
    assert resp.json()["payment_id"] == 7
