"""Regression coverage for the apply-payment internal-service header and for
_apply_via_servicing's return contract.

servicing-service's /accounts/{loan_id}/apply-payment now requires
X-Internal-Service (ADR 0014 Decision 1, services/servicing-service/app/authz.py
require_internal_caller). Two Codex review rounds on PR 32 found this in stages:
first, payment-service sent no such header at all (fixed by sending it); then,
even with the header sent, a REJECTED apply (e.g. mismatched
INTERNAL_SERVICE_TOKEN between the two services) was swallowed identically to
servicing being merely unreachable, so charge() always reported status
"captured" regardless of whether the balance actually moved. This file proves
both the client sends the header AND that a rejection is reported honestly.
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


def test_apply_via_servicing_unreachable_still_reports_applied(monkeypatch):
    # Network-level failure (unreachable/timeout/DNS) is NOT the same as a
    # rejection: the card was already charged, so this stays "captured" and
    # reconciled later (D7) -- the original documented design for this case.
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(payments, "INTERNAL_SERVICE_TOKEN", "sekret")

    def fake_post(url, json=None, headers=None, timeout=None):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(payments.httpx, "post", fake_post)

    applied = payments._apply_via_servicing(loan_id=1, amount=50.0, payment_id=7)

    assert applied is True


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
