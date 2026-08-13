"""Regression coverage for the apply-payment internal-service header.

servicing-service's /accounts/{loan_id}/apply-payment now requires
X-Internal-Service (ADR 0014 Decision 1, services/servicing-service/app/authz.py
require_internal_caller). Before this fix, payment-service's _apply_via_servicing
posted no such header: servicing denied the call with 403, _apply_via_servicing
swallowed the exception (servicing-unreachable handling), and charge() still
returned status "captured" -- a customer would be charged with their loan
balance never reduced. See services/servicing-service/app/authz.py::
require_internal_caller and test_authz.py's own internal-caller coverage for the
server-side half of this gate; this file proves the client half.
"""

from app import config, payments


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


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

    payments._apply_via_servicing(loan_id=1, amount=50.0, payment_id=7)

    assert captured["headers"] == {"X-Internal-Service": "sekret"}
    assert captured["json"] == {"amount": 50.0, "payment_id": 7}


def test_apply_via_servicing_without_token_would_be_denied_by_servicing(monkeypatch):
    # Simulates servicing's real gate (require_internal_caller: no token configured
    # here means the header sent is empty, which servicing's hmac.compare_digest
    # check rejects same as a missing header) -- proves the old missing-header bug
    # is caught by a status check, not silently swallowed as "captured".
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "")
    monkeypatch.setattr(payments, "INTERNAL_SERVICE_TOKEN", "")

    def fake_post(url, json=None, headers=None, timeout=None):
        assert headers == {"X-Internal-Service": ""}
        return _FakeResponse(403)  # what servicing's real gate returns

    monkeypatch.setattr(payments.httpx, "post", fake_post)

    errors = []
    monkeypatch.setattr(payments.log, "error", lambda *a, **k: errors.append((a, k)))

    payments._apply_via_servicing(loan_id=1, amount=50.0, payment_id=7)

    assert errors, "a denied apply-payment call must be logged, not silently ignored"
