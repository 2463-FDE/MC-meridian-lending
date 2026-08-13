"""Regression coverage for the apply-payment internal-service header and for
_apply_via_servicing's return contract.

servicing-service's /accounts/{loan_id}/apply-payment now requires
X-Internal-Service (ADR 0014 Decision 1, services/servicing-service/app/authz.py
require_internal_caller). Three Codex review rounds on PR 32 found this in stages:
first, payment-service sent no such header at all; then, a REJECTED or REDIRECTED
apply was swallowed identically to success; then, an UNREACHABLE servicing call
was still reported as a successful apply on the reasoning it would be
"reconciled later" -- which nothing in this codebase actually does
(reconciliation.py's reconciliation_peek runs on no schedule and reports no
breaks, D7). _apply_via_servicing now returns True only on an actual confirmed
2xx; every other outcome (missing header, rejected, redirected, unreachable) is
False, and charge() reports "captured_unapplied" rather than lying about the
balance having moved.
"""

from app import config, payments


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


def test_apply_via_servicing_sends_internal_service_header(monkeypatch):
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(payments, "INTERNAL_SERVICE_TOKEN", "sekret")

    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(200)

    monkeypatch.setattr(payments.httpx, "post", fake_post)

    applied = payments._apply_via_servicing(loan_id=1, amount=50.0, payment_id=7)

    assert applied is True
    assert captured["headers"] == {"X-Internal-Service": "sekret"}
    assert captured["json"] == {"amount": 50.0, "payment_id": 7}


def test_apply_via_servicing_rejected_reports_not_applied(monkeypatch):
    # Simulates servicing's real gate (require_internal_caller: no token configured
    # here means the header sent is empty, which servicing's hmac.compare_digest
    # check rejects same as a missing header) -- proves a REJECTED apply is now a
    # first-class False, not silently swallowed and treated as success.
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "")
    monkeypatch.setattr(payments, "INTERNAL_SERVICE_TOKEN", "")

    def fake_post(url, json=None, headers=None, timeout=None):
        assert headers == {"X-Internal-Service": ""}
        return _FakeResponse(403)  # what servicing's real gate returns

    monkeypatch.setattr(payments.httpx, "post", fake_post)

    errors = []
    monkeypatch.setattr(payments.log, "error", lambda *a, **k: errors.append((a, k)))

    applied = payments._apply_via_servicing(loan_id=1, amount=50.0, payment_id=7)

    assert applied is False
    assert errors, "a denied apply-payment call must be logged, not silently ignored"


def test_apply_via_servicing_redirect_reports_not_applied(monkeypatch):
    # httpx does not follow redirects by default: a 3xx means the apply-payment
    # handler on servicing never ran, same as a 403 -- checking only
    # `status_code >= 400` let this fall through to the success branch
    # (Codex review, PR 32, second round).
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(payments, "INTERNAL_SERVICE_TOKEN", "sekret")

    def fake_post(url, json=None, headers=None, timeout=None):
        return _FakeResponse(307)

    monkeypatch.setattr(payments.httpx, "post", fake_post)

    applied = payments._apply_via_servicing(loan_id=1, amount=50.0, payment_id=7)

    assert applied is False


def test_apply_via_servicing_unreachable_reports_not_applied(monkeypatch):
    # A network-level failure (unreachable/timeout/DNS) leaves the balance just
    # as unupdated as a rejection does. An earlier version of this fix reported
    # this case as still "captured" on the reasoning that it would be
    # "reconciled later" -- but reconciliation.py's reconciliation_peek is
    # explicitly not run on a schedule and does not report breaks (D7), so
    # nothing in this codebase actually reconciles it. Treat it the same as a
    # rejection (Codex review, PR 32, third round).
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(payments, "INTERNAL_SERVICE_TOKEN", "sekret")

    def fake_post(url, json=None, headers=None, timeout=None):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(payments.httpx, "post", fake_post)

    errors = []
    monkeypatch.setattr(payments.log, "error", lambda *a, **k: errors.append((a, k)))

    applied = payments._apply_via_servicing(loan_id=1, amount=50.0, payment_id=7)

    assert applied is False
    assert errors, (
        "an unreachable apply-payment call must be logged, not silently ignored"
    )


def test_charge_reports_captured_unapplied_when_servicing_rejects(monkeypatch):
    # End-to-end through charge(): a real charge with a rejected apply must not
    # come back reporting a normal successful capture.
    monkeypatch.setattr(payments.db, "query", lambda *a, **k: [{"id": 1}])
    monkeypatch.setattr(payments, "_apply_via_servicing", lambda *a, **k: False)

    out = payments.charge(loan_id=1, pan="4111111111111111", cvv="123", amount=50.0)

    assert out["status"] == "captured_unapplied"
    assert out["applied_amount"] == 0.0


def test_charge_reports_captured_when_servicing_applies(monkeypatch):
    monkeypatch.setattr(payments.db, "query", lambda *a, **k: [{"id": 1}])
    monkeypatch.setattr(payments, "_apply_via_servicing", lambda *a, **k: True)

    out = payments.charge(loan_id=1, pan="4111111111111111", cvv="123", amount=50.0)

    assert out["status"] == "captured"
    assert out["applied_amount"] == 50.0
