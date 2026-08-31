"""Regression coverage for D1: one correlation id across the payment span.

Test vectors V-TRACE, V-TRACE-SUPPLIED and V-TRACE-FAIL from
docs/specs/observability-week7.md, plus acceptance criteria 3 (no log line
asserts success before the work it describes) and 9b (every payment-path line
carries request_id, loan_id, payment_id and outcome as NAMED fields, in a fixed
order, so a span is recoverable by field extraction rather than by reading
prose).

The id is called request_id, matching decision-service's existing convention --
not trace_id and not correlation_id (spec D1(a)).

These are structured FIELDS inside the ordinary text log line. The formatter
stays RedactingFormatter("%(levelname)s %(asctime)s %(message)s") and the PII
redactor keeps scanning the same formatted byte sequence; re-encoding as JSON
would change what two blocking gates (redaction-tests, redactor-drift) scan and
is explicitly out of scope for this week.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app import authz, config, payments
from app.main import app


# The charge route now fails closed on an unready schema (D13a: a volume that skipped
# migration 0020 still holds every stored CVV, and the NULLABLE legacy column lets the
# capture insert succeed anyway). These cases have no database at all, so the probe would
# refuse every request and grade nothing but the guard. The guard itself is graded in
# test_no_sad.py (unmigrated volume) and test_db_readiness.py (the rungs).
@pytest.fixture(autouse=True)
def _schema_ready(monkeypatch):
    monkeypatch.setattr(config, "database_reachable", lambda *a, **k: (True, None))


# D19: the Idempotency-Key is required and client-minted (ADR 0013 Decision 1), so every
# call into the charge path has to carry one. A fixed valid UUID keeps these cases
# deterministic; the idempotency behaviour itself is covered by the R-vector suite.
_IDEM_KEY = "11111111-1111-4111-8111-111111111111"


# request_id, loan_id, payment_id, outcome -- the documented order (spec D1(c)).
FIELDS = re.compile(
    r"request_id=(?P<request_id>\S+) "
    r"loan_id=(?P<loan_id>\S+) "
    r"payment_id=(?P<payment_id>\S+) "
    r"outcome=(?P<outcome>\S+)"
)


def _capture_log(monkeypatch):
    """Collect fully-interpolated log lines, the way the formatter sees them."""
    lines = []

    def record(msg, *args, **kwargs):
        lines.append(msg % args if args else msg)

    monkeypatch.setattr(payments.log, "info", record)
    monkeypatch.setattr(payments.log, "error", record)
    return lines


def _fields(lines):
    """The parsed field-sets of every line that carries the field block."""
    return [m.groupdict() for m in (FIELDS.search(line) for line in lines) if m]


def _stub_charge_path(monkeypatch, *, status_code=200, insert_id=7):
    """db INSERT returns insert_id; servicing returns status_code. Captures headers."""
    captured = {}
    monkeypatch.setattr(payments, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(payments.db, "query", lambda *a, **k: [{"id": insert_id}])

    class _FakeResponse:
        def __init__(self, code):
            self.status_code = code

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["headers"] = headers
        captured["json"] = json
        return _FakeResponse(status_code)

    monkeypatch.setattr(payments.httpx, "post", fake_post)
    return captured


def test_supplied_request_id_used_verbatim_on_every_line(monkeypatch):
    """V-TRACE-SUPPLIED: a caller-supplied id is used verbatim on the charge
    line, the outcome line, and on the header servicing logs its own line from."""
    captured = _stub_charge_path(monkeypatch)
    lines = _capture_log(monkeypatch)

    payments.charge(
        loan_id=1,
        pan="4111111111111111",
        amount=50.0,
        request_id="abc123",
        idempotency_key=_IDEM_KEY,
    )

    parsed = _fields(lines)
    assert parsed, f"no line carried the named field block: {lines}"
    assert {p["request_id"] for p in parsed} == {"abc123"}, (
        f"supplied id must be used verbatim on every line: {lines}"
    )
    assert captured["headers"]["X-Request-Id"] == "abc123"


def test_generated_request_id_is_one_id_shared_by_the_whole_span(monkeypatch):
    """V-TRACE: with no supplied id, ONE generated id appears on every line and
    on the propagated header -- not a fresh id per line."""
    captured = _stub_charge_path(monkeypatch)
    lines = _capture_log(monkeypatch)

    payments.charge(
        loan_id=1, pan="4111111111111111", amount=50.0, idempotency_key=_IDEM_KEY
    )

    ids = {p["request_id"] for p in _fields(lines)}
    assert len(ids) == 1, f"the span must share one id, got {ids}: {lines}"
    generated = ids.pop()
    assert re.fullmatch(r"[a-p]{32}", generated), (
        f"expected a digit-mapped uuid4 hex (letters only), got {generated!r}"
    )
    assert captured["headers"]["X-Request-Id"] == generated


def test_unreachable_failure_line_carries_the_same_id(monkeypatch):
    """V-TRACE-FAIL: servicing unreachable -- the failure line and the charge
    line share one id, so a captured-unapplied payment is findable by field."""
    monkeypatch.setattr(payments, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(payments.db, "query", lambda *a, **k: [{"id": 7}])

    def fake_post(url, json=None, headers=None, timeout=None):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(payments.httpx, "post", fake_post)
    lines = _capture_log(monkeypatch)

    out = payments.charge(
        loan_id=1,
        pan="4111111111111111",
        amount=50.0,
        request_id="abc123",
        idempotency_key=_IDEM_KEY,
    )

    assert out["status"] == "captured_unapplied"
    parsed = _fields(lines)
    assert {p["request_id"] for p in parsed} == {"abc123"}, lines
    outcomes = [p["outcome"] for p in parsed]
    assert "captured_unapplied" in outcomes, (
        f"a failed apply must be distinguishable in the logs (criterion 2): {lines}"
    )
    assert "captured" not in outcomes, (
        f"a failed apply must not also log a success outcome: {lines}"
    )


def test_no_line_claims_success_before_the_insert(monkeypatch):
    """Criterion 3 / D1(d): the entry line is logged BEFORE the INSERT, so it
    must not assert an outcome the row does not yet have. The pre-D1 code logged
    'POST /payments charge req=... -> ok' at that point."""
    monkeypatch.setattr(payments, "INTERNAL_SERVICE_TOKEN", "sekret")

    def exploding_insert(*a, **k):
        raise RuntimeError("INSERT failed")

    monkeypatch.setattr(payments.db, "query", exploding_insert)
    lines = _capture_log(monkeypatch)

    try:
        payments.charge(
            loan_id=1,
            pan="4111111111111111",
            amount=50.0,
            request_id="abc123",
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


def test_every_payment_path_line_carries_the_four_named_fields(monkeypatch):
    """Criterion 9b: every line on the charge path carries all four fields in
    the documented order -- the span is recoverable by extraction alone."""
    _stub_charge_path(monkeypatch)
    lines = _capture_log(monkeypatch)

    payments.charge(
        loan_id=1,
        pan="4111111111111111",
        amount=50.0,
        request_id="abc123",
        idempotency_key=_IDEM_KEY,
    )

    assert len(_fields(lines)) == len(lines), (
        f"every line must carry the four fields in order: {lines}"
    )
    outcomes = [p["outcome"] for p in _fields(lines)]
    assert outcomes[0] == "started"
    assert "captured" in outcomes
    # The entry line precedes the INSERT, so it has no payment id yet: it says so
    # rather than omitting the field, same reason servicing logs request_id=-.
    assert _fields(lines)[0]["payment_id"] == "-"
    assert all(p["loan_id"] == "1" for p in _fields(lines)), lines


def test_route_forwards_the_x_request_id_header_to_charge(monkeypatch):
    """D1(a): POST /payments accepts X-Request-Id and hands it to charge(). The
    route is where the header enters the span."""
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "sekret")
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(authz, "require_money_role_or_owner", lambda *a, **k: None)

    seen = {}

    def fake_charge(loan_id, pan, amount, ssn, name, method, request_id=None, **kwargs):
        seen["request_id"] = request_id
        return {
            "payment_id": 1,
            "loan_id": loan_id,
            "status": "captured",
            "applied_amount": amount,
        }

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


def test_hostile_request_id_is_not_logged_verbatim(monkeypatch):
    """The header is client-controlled free text on its way into a log line. A
    newline forges a whole log record; a digit run smuggles a card number past
    the value-level masking in _redacted_charge_req. 'Used verbatim' (D1(a))
    covers a real id, so an id outside the accepted charset is replaced by a
    generated one rather than written through."""
    _stub_charge_path(monkeypatch)
    lines = _capture_log(monkeypatch)

    payments.charge(
        loan_id=1,
        pan="4111111111111111",
        amount=50.0,
        request_id="abc\nINFO 2026-08-13 forged line 4111111111111111",
        idempotency_key=_IDEM_KEY,
    )

    joined = "\n".join(lines)
    assert "forged line" not in joined, f"log injection via X-Request-Id: {lines}"
    ids = {p["request_id"] for p in _fields(lines)}
    assert len(ids) == 1
    assert re.fullmatch(r"[a-p]{32}", ids.pop()), (
        f"a rejected id must fall back to a generated one, not to empty: {lines}"
    )


# The accepted charset alone lets a bare SSN and a card-length digit run through,
# and the redactor does not catch either inside request_id=...: it masks a bare
# SSN only inside a LABELED field and a bare PAN only when Luhn-VALID. Each value
# below therefore has to be refused at validation, and refused on BOTH exits --
# this service's own log line AND the X-Request-Id header propagated to servicing,
# which logs whatever it is handed. (Codex review)
PII_SHAPED_REQUEST_IDS = [
    ("bare_ssn", "412559981"),
    ("dashed_ssn", "412-55-9981"),
    ("separator_padded_ssn", "4.1.2.5.5.9.9.8.1"),
    ("invalid_luhn_pan", "4111111111111112"),
    ("letter_padded_invalid_luhn_pan", "4111a1111a1111a1112"),
    ("epoch_millis", "1755180000000"),
]


def test_pii_shaped_request_id_reaches_neither_the_log_nor_servicing(monkeypatch):
    """A supplied id carrying SSN- or card-length digits is replaced, so the raw
    digits appear in no log line and in no propagated header."""
    for label, hostile in PII_SHAPED_REQUEST_IDS:
        captured = _stub_charge_path(monkeypatch)
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
        assert captured["headers"]["X-Request-Id"] != hostile, (
            f"{label}: PII-shaped id propagated to servicing, which logs it"
        )
        ids = {p["request_id"] for p in _fields(lines)}
        assert len(ids) == 1, f"{label}: the span must still share one id, got {ids}"
        generated = ids.pop()
        assert re.fullmatch(r"[a-p]{32}", generated), (
            f"{label}: a refused id must fall back to a generated one: {lines}"
        )
        assert captured["headers"]["X-Request-Id"] == generated, (
            f"{label}: both halves of the span must still carry the same id"
        )


def test_a_real_id_with_a_few_digits_is_still_used_verbatim():
    """The ceiling is on SSN/card-length digit runs, not on digits: an ordinary
    id with fewer than nine digits keeps D1(a)'s verbatim guarantee."""
    # 8 digits, one under the ceiling -- the boundary the rule is written on.
    for ok in ("abc123", "pay-2026-08-a1b2", "req.7", "a" * 64):
        assert payments.new_request_id(ok) == ok, ok


def test_replaced_request_id_is_returned_to_the_caller(monkeypatch):
    """A digit-heavy caller id (e.g. epoch-millis) is silently replaced by
    new_request_id -- but charge() must hand the effective id back in its
    result, or the caller and gateway have no way to learn what the payment
    and servicing logs are keyed on (Codex review)."""
    _stub_charge_path(monkeypatch)

    hostile = "1755180000000"  # 13-digit epoch-millis id, over the ceiling
    result = payments.charge(
        loan_id=1,
        pan="4111111111111111",
        amount=50.0,
        request_id=hostile,
        idempotency_key=_IDEM_KEY,
    )

    assert "request_id" in result, "caller has no way to recover the effective id"
    assert result["request_id"] != hostile
    assert re.fullmatch(r"[a-p]{32}", result["request_id"])


# Mirrors servicing-service's _span_request_id validation
# (services/servicing-service/app/main.py) exactly: the same charset and the
# same 9-digit ceiling, since that route is reachable directly and
# re-validates every X-Request-Id rather than trusting the header. A raw
# uuid4 hex clears the charset but fails the digit ceiling near-certainly (32
# hex chars average ~20 digits), so before the fix a generated id was logged
# in full here but replaced with "-" on servicing's line for every no-header
# or refused-header request -- the two halves of the span stopped sharing an
# id (Codex review, PR 41).
_SERVICING_REQUEST_ID_OK = re.compile(r"[A-Za-z0-9._-]{1,64}")
_SERVICING_MAX_DIGITS = 9


def test_generated_request_id_survives_servicings_independent_revalidation():
    """The id minted with no caller header, or a refused one, must clear
    servicing's own re-application of the digit ceiling -- not just this
    service's. Run many times: the fallback is random (uuid4-derived), and a
    single generation passing by chance would not prove the invariant."""
    for _ in range(50):
        generated = payments.new_request_id()
        assert _SERVICING_REQUEST_ID_OK.fullmatch(generated), generated
        digits = len(re.findall(r"[0-9]", generated))
        assert digits < _SERVICING_MAX_DIGITS, (
            f"generated id {generated!r} carries {digits} digits -- "
            "servicing-service will replace it with '-', breaking span "
            "correlation for this request"
        )
