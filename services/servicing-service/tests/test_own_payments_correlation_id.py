"""Regression coverage for D1 on servicing-service's OWN POST /payments route.

This route (app.main.post_payment -> app.payments.charge) is a second front
door for the same charge path as payment-service's /payments -- ADR 0004
decomposition left the original pre-split implementation live here. The teeth
review on PR 41 found it carried none of the D1 fixes applied to
payment-service's copy: no request_id at all, and the same "-> ok before the
INSERT" false-success line criterion 3 explicitly fixed on the other side of
this identical route shape. This file closes both.
"""

import re

from fastapi.testclient import TestClient

from app import authz, balance, config, payments
from app.main import app

# D19: the Idempotency-Key is required and client-minted (ADR 0013 Decision 1), so every
# call into the charge path has to carry one. A fixed valid UUID keeps these cases
# deterministic; the idempotency behaviour itself is covered by the R-vector suite.
_IDEM_KEY = "11111111-1111-4111-8111-111111111111"


FIELDS = re.compile(
    r"request_id=(?P<request_id>\S+) "
    r"loan_id=(?P<loan_id>\S+) "
    r"payment_id=(?P<payment_id>\S+) "
    r"outcome=(?P<outcome>\S+)"
)


def _capture_log(monkeypatch):
    lines = []

    def record(msg, *args, **kwargs):
        lines.append(msg % args if args else msg)

    monkeypatch.setattr(payments.log, "info", record)
    monkeypatch.setattr(payments.log, "error", record)
    return lines


def _fields(lines):
    return [m.groupdict() for m in (FIELDS.search(line) for line in lines) if m]


def _stub_charge_path(monkeypatch, insert_id=7):
    monkeypatch.setattr(payments.db, "query", lambda *a, **k: [{"id": insert_id}])
    monkeypatch.setattr(
        payments.balance, "apply_payment", lambda *a, **k: (450.0, True)
    )


def test_no_line_claims_success_before_the_insert(monkeypatch):
    """Criterion 3 / D1(d): the entry line must not assert an outcome the row
    does not yet have. The pre-fix code logged 'POST /payments charge req=...
    -> ok' at exactly this position, before the INSERT ran."""

    def exploding_insert(*a, **k):
        raise RuntimeError("INSERT failed")

    monkeypatch.setattr(payments.db, "query", exploding_insert)
    lines = _capture_log(monkeypatch)

    try:
        payments.charge(
            loan_id=1,
            pan="4111111111111111",
            amount=50.0,
            idempotency_key=_IDEM_KEY,
        )
    except RuntimeError:
        pass

    assert lines, "the span must still open with an entry line"
    assert not any("-> ok" in line for line in lines), (
        f"no line may assert success for work that never happened: {lines}"
    )
    outcomes = [p["outcome"] for p in _fields(lines)]
    assert outcomes == ["started"], (
        f"the entry line reports 'started', not an outcome it cannot know: {lines}"
    )


def test_generated_request_id_is_one_id_shared_by_entry_and_outcome(monkeypatch):
    """With no supplied id, one generated id appears on both the entry and
    outcome lines -- not a fresh id per line."""
    _stub_charge_path(monkeypatch)
    lines = _capture_log(monkeypatch)

    result = payments.charge(
        loan_id=1,
        pan="4111111111111111",
        amount=50.0,
        idempotency_key=_IDEM_KEY,
    )

    ids = {p["request_id"] for p in _fields(lines)}
    assert len(ids) == 1, f"the span must share one id, got {ids}: {lines}"
    generated = ids.pop()
    assert re.fullmatch(r"[a-p]{32}", generated), (
        f"expected a digit-mapped uuid4 hex (letters only), got {generated!r}"
    )
    assert result["request_id"] == generated
    assert result["payment_id"] == 7


def test_supplied_request_id_used_verbatim(monkeypatch):
    _stub_charge_path(monkeypatch)
    lines = _capture_log(monkeypatch)

    payments.charge(
        loan_id=1,
        pan="4111111111111111",
        amount=50.0,
        request_id="abc123",
        idempotency_key=_IDEM_KEY,
    )

    ids = {p["request_id"] for p in _fields(lines)}
    assert ids == {"abc123"}, lines


def test_route_forwards_the_x_request_id_header_to_charge(monkeypatch):
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "sekret")
    monkeypatch.setattr(authz, "require_money_role_or_owner", lambda *a, **k: None)

    seen = {}

    def fake_charge(loan_id, pan, amount, ssn, name, method, request_id=None, **kwargs):
        seen["request_id"] = request_id
        return {"loan_id": loan_id, "amount": amount, "balance": 0.0}

    monkeypatch.setattr("app.payments.charge", fake_charge)

    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 50.0},
        headers={
            "Idempotency-Key": _IDEM_KEY,
            "X-User-Role": "csr",
            "X-Request-Id": "abc123",
        },
    )

    assert resp.status_code == 200
    assert seen["request_id"] == "abc123"


# Mirrors payment-service's PII_SHAPED_REQUEST_IDS -- this front door captures
# a PAN/CVV/SSN directly, same as payment-service's, so the same ceiling has
# to hold here too.
PII_SHAPED_REQUEST_IDS = [
    ("bare_ssn", "412559981"),
    ("invalid_luhn_pan", "4111111111111112"),
]


def test_pii_shaped_request_id_reaches_no_log_line(monkeypatch):
    for label, hostile in PII_SHAPED_REQUEST_IDS:
        _stub_charge_path(monkeypatch)
        lines = _capture_log(monkeypatch)

        payments.charge(
            loan_id=1,
            pan="4111111111111111",
            amount=50.0,
            request_id=hostile,
            idempotency_key=_IDEM_KEY,
        )

        joined = "\n".join(lines)
        assert hostile not in joined, f"{label}: PII-shaped id logged raw: {lines}"
        ids = {p["request_id"] for p in _fields(lines)}
        assert re.fullmatch(r"[a-p]{32}", next(iter(ids))), (
            f"{label}: a refused id must fall back to a generated one: {lines}"
        )


def test_balance_apply_payment_own_log_line_carries_the_span(monkeypatch):
    """Review comment (PR 50): balance.apply_payment's own log line -- the
    actual balance mutation, distinct from payments.py's entry/outcome lines
    around it -- previously carried none of the four span fields, and
    _stub_charge_path replaced apply_payment with a lambda so that real line
    never ran. Run the real helper (only its db.query is stubbed) and assert
    its own line, not the caller's."""
    insert_id = 7

    def fake_query(sql, params=None):
        if sql.strip().startswith("INSERT INTO payments"):
            return [{"id": insert_id}]
        # The D3 apply is one statement that answers with the row it wrote.
        if "payment_applications" in sql and sql.strip().startswith("WITH"):
            return [{"loan_id": 1, "balance": 450.0, "amount_minor": 5000}]
        if sql.strip().startswith("SELECT balance"):
            return [{"balance": 500.0}]
        return []

    monkeypatch.setattr(payments.db, "query", fake_query)
    monkeypatch.setattr(balance.db, "query", fake_query)
    lines = []
    monkeypatch.setattr(
        balance.log, "info", lambda msg, *a, **k: lines.append(msg % a if a else msg)
    )

    result = payments.charge(
        loan_id=1,
        pan="4111111111111111",
        amount=50.0,
        request_id="abc123",
        idempotency_key=_IDEM_KEY,
    )

    parsed = _fields(lines)
    assert parsed, f"balance.apply_payment must log the span fields: {lines}"
    assert parsed[0]["request_id"] == "abc123"
    assert parsed[0]["loan_id"] == "1"
    assert parsed[0]["payment_id"] == str(insert_id) == str(result["payment_id"])
    # `applied`, not `captured`, since D3: the outcome on the mutation's own line is
    # now derived from what the statement did (applied / already_applied) rather than
    # passed in by the caller, which is the point of the record being the source of
    # truth. The caller's own entry/outcome lines still say captured.
    assert parsed[0]["outcome"] == "applied"


def test_replay_of_a_row_captured_unapplied_by_the_other_writer_reports_zero(
    monkeypatch,
):
    """B1 sibling: this route is a second writer of the SAME payments table (D23).

    A row this route replays may have been captured_unapplied by payment-service's
    handler -- `amount` is what the CARD captured, not what the balance absorbed, so
    reporting it on a replay of an unapplied row would tell the caller money moved
    that never did.
    """
    monkeypatch.setattr(
        payments,
        "claim_or_branch",
        lambda *a, **k: (
            payments.REPLAY,
            7,
            {"status": "captured_unapplied", "amount": 250.0},
        ),
    )

    result = payments.charge(
        loan_id=1,
        pan="4111111111111111",
        amount=250.0,
        idempotency_key=_IDEM_KEY,
    )

    assert result["idempotency"] == payments.REPLAY
    assert result["amount"] == 0.0
