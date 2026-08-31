"""Regression coverage for D1 on the LSS half of the payment span.

apply_payment is the only place a captured charge becomes a balance movement,
and before this it logged NOTHING -- so a payment that crossed the seam left one
line in payment-service and no counterpart here at all.

Test vector V-TRACE-DIRECT (docs/specs/observability-week7.md): a direct call
with no X-Request-Id is logged as request_id=-, not with the field omitted, so
an uncorrelated apply is visible as uncorrelated rather than silently
untraceable.
"""

import re

import pytest
from fastapi.testclient import TestClient

from app import balance, config, main
from app.main import app


# The charge route now fails closed on an unready schema (D13a: a volume that skipped
# migration 0020 still holds every stored CVV, and the NULLABLE legacy column lets the
# capture insert succeed anyway). These cases have no database at all, so the probe would
# refuse every request and grade nothing but the guard. The guard itself is graded in
# test_no_sad.py (unmigrated volume) and test_db_readiness.py (the rungs).
@pytest.fixture(autouse=True)
def _schema_ready(monkeypatch):
    monkeypatch.setattr(config, "database_reachable", lambda *a, **k: (True, None))


FIELDS = re.compile(
    r"request_id=(?P<request_id>\S+) "
    r"loan_id=(?P<loan_id>\S+) "
    r"payment_id=(?P<payment_id>\S+) "
    r"outcome=(?P<outcome>\S+)"
)


def _capture_log(monkeypatch):
    """Capture both main.log (the route handler) and balance.log (a distinct
    logging.getLogger instance -- get_logger("servicing") vs get_logger("balance")
    -- so a stray unlabeled line inside balance.apply_payment is not invisible to
    this suite just because the two loggers are different objects."""
    lines = []

    def record(msg, *args, **kwargs):
        lines.append(msg % args if args else msg)

    monkeypatch.setattr(main.log, "info", record)
    monkeypatch.setattr(main.log, "error", record)
    monkeypatch.setattr(balance.log, "info", record)
    monkeypatch.setattr(balance.log, "error", record)
    return lines


def _stub_db_query(sql, params=None):
    """Stand in for app.db.query over a one-row balances table, so the real
    balance.apply_payment runs (not a mock that would hide its own logging).

    Since D3 the apply is one statement (WITH ... UPDATE ... RETURNING) that answers
    with the row it wrote, so the stub answers in that shape: an eligible $50 payment
    against an opening $500."""
    upper = sql.upper()
    if "PAYMENT_APPLICATIONS" in upper and upper.strip().startswith("WITH"):
        return [{"loan_id": 1, "balance": 450.0, "amount_minor": 5000}]
    if upper.strip().startswith("SELECT"):
        return [{"balance": 500.0}]
    return []


def _apply(monkeypatch, headers):
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr("app.balance.db.query", _stub_db_query)
    lines = _capture_log(monkeypatch)
    resp = TestClient(app).post(
        "/accounts/1/apply-payment",
        json={"payment_id": 7},
        headers={"X-Internal-Service": "sekret", **headers},
    )
    assert resp.status_code == 200
    return [m.groupdict() for m in (FIELDS.search(x) for x in lines) if m], lines


def test_apply_payment_logs_the_supplied_request_id(monkeypatch):
    """The id payment-service propagates lands on servicing's line verbatim, so
    both halves of the span carry one id."""
    parsed, lines = _apply(monkeypatch, {"X-Request-Id": "abc123"})

    assert parsed, f"apply_payment must log a line with the named fields: {lines}"
    assert parsed[0]["request_id"] == "abc123"
    assert parsed[0]["loan_id"] == "1"
    assert parsed[0]["payment_id"] == "7"
    assert parsed[0]["outcome"] == "applied"


def test_every_line_on_a_successful_apply_carries_the_field_block(monkeypatch):
    """balance.apply_payment used to log an unlabeled 'applied payment loan_id=...'
    line with no request_id/payment_id/outcome, so a request_id-scoped log search
    missed it -- the exact gap this span exists to close. Runs the real balance
    function (not a mock of it) so that line, if reintroduced, is caught here."""
    parsed, lines = _apply(monkeypatch, {"X-Request-Id": "abc123"})

    assert lines, "apply_payment must log at least one line"
    assert len(parsed) == len(lines), (
        f"every log line on the apply-payment path must carry the "
        f"request_id/loan_id/payment_id/outcome field block: {lines}"
    )


def test_direct_call_without_header_is_logged_as_uncorrelated(monkeypatch):
    """V-TRACE-DIRECT: no header -- the field is present with '-', never
    omitted. An omitted field reads as a parse gap; '-' reads as a fact."""
    parsed, lines = _apply(monkeypatch, {})

    assert parsed, f"apply_payment must log a line with the named fields: {lines}"
    assert parsed[0]["request_id"] == "-", lines


def test_hostile_request_id_is_not_logged_verbatim(monkeypatch):
    """Same header, same client-controlled free text, same charset gate as
    payment-service: a newline in the header must not forge a log record here
    either. Servicing has no id to fall back to, so a rejected value is
    logged as the uncorrelated marker."""
    parsed, lines = _apply(
        monkeypatch,
        {"X-Request-Id": "abc def"},  # a space alone already splits the field
    )

    assert parsed[0]["request_id"] == "-", lines
    assert "abc def" not in "\n".join(lines), lines


# Mirrors payment-service's PII_SHAPED_REQUEST_IDS. This route is reachable
# directly with only the internal token, so the digit ceiling has to hold here
# too -- servicing cannot assume the id was validated upstream. The redactor
# masks a bare SSN only inside a labeled field and a bare PAN only when
# Luhn-valid, so neither shape is caught inside request_id=... (Codex review)
PII_SHAPED_REQUEST_IDS = [
    ("bare_ssn", "412559981"),
    ("dashed_ssn", "412-55-9981"),
    ("separator_padded_ssn", "4.1.2.5.5.9.9.8.1"),
    ("invalid_luhn_pan", "4111111111111112"),
    ("letter_padded_invalid_luhn_pan", "4111a1111a1111a1112"),
]


def test_pii_shaped_request_id_is_logged_as_uncorrelated(monkeypatch):
    """A header carrying SSN- or card-length digits is refused like any other
    unusable value: the line says request_id=- and holds none of the digits."""
    for label, hostile in PII_SHAPED_REQUEST_IDS:
        parsed, lines = _apply(monkeypatch, {"X-Request-Id": hostile})

        assert parsed[0]["request_id"] == "-", f"{label}: {lines}"
        assert hostile not in "\n".join(lines), (
            f"{label}: PII-shaped id logged raw: {lines}"
        )


def test_a_real_id_with_a_few_digits_is_still_logged_verbatim(monkeypatch):
    """The ceiling is on SSN/card-length digit runs, not on digits: an id with
    fewer than nine still correlates the two halves of the span."""
    parsed, lines = _apply(monkeypatch, {"X-Request-Id": "pay-2026-08-a1b2"})

    assert parsed[0]["request_id"] == "pay-2026-08-a1b2", lines


def test_balance_apply_payment_own_log_line_carries_the_span(monkeypatch):
    """Review comment (PR 50): balance.apply_payment's own log line -- the
    actual balance mutation, not the handler's line around it -- previously
    carried none of the four span fields, and every existing test replaced
    apply_payment with a lambda so that real line never ran. Run the real
    helper (only its db.query is stubbed) and assert its own line, not the
    handler's."""
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")

    def fake_query(sql, params=None):
        if "payment_applications" in sql and sql.strip().startswith("WITH"):
            return [{"loan_id": 1, "balance": 450.0, "amount_minor": 5000}]
        if sql.strip().startswith("SELECT balance"):
            return [{"balance": 500.0}]
        return []

    monkeypatch.setattr(balance.db, "query", fake_query)
    lines = []
    monkeypatch.setattr(
        balance.log, "info", lambda msg, *a, **k: lines.append(msg % a if a else msg)
    )

    resp = TestClient(app).post(
        "/accounts/1/apply-payment",
        json={"payment_id": 7},
        headers={"X-Internal-Service": "sekret", "X-Request-Id": "abc123"},
    )
    assert resp.status_code == 200

    parsed = [m.groupdict() for m in (FIELDS.search(x) for x in lines) if m]
    assert parsed, f"balance.apply_payment must log the span fields: {lines}"
    assert parsed[0]["request_id"] == "abc123"
    assert parsed[0]["loan_id"] == "1"
    assert parsed[0]["payment_id"] == "7"
    assert parsed[0]["outcome"] == "applied"
