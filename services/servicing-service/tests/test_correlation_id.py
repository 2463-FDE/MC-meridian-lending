"""Regression coverage for D1 on the LSS half of the payment span.

apply_payment is the only place a captured charge becomes a balance movement,
and before this it logged NOTHING -- so a payment that crossed the seam left one
line in payment-service and no counterpart here at all.

Test vector V-TRACE-DIRECT (docs/spec-observability-week7.md): a direct call
with no X-Request-Id is logged as request_id=-, not with the field omitted, so
an uncorrelated apply is visible as uncorrelated rather than silently
untraceable.
"""

import re

from fastapi.testclient import TestClient

from app import config, main
from app.main import app

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

    monkeypatch.setattr(main.log, "info", record)
    monkeypatch.setattr(main.log, "error", record)
    return lines


def _apply(monkeypatch, headers):
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr("app.balance.apply_payment", lambda loan_id, amount: 50.0)
    lines = _capture_log(monkeypatch)
    resp = TestClient(app).post(
        "/accounts/1/apply-payment",
        json={"amount": 50.0, "payment_id": 7},
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
