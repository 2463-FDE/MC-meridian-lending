"""D1 cross-service regression: the id payment-service returns from a charge
actually appears, verbatim, on servicing-service's OWN log line -- not just on
two independently-maintained copies of the same validation rule.

Comment (PR 41): "Add an end-to-end test where POST /payments has no
X-Request-Id and asserts servicing logs the same effective request_id
returned by payment-service." The prior suite
(services/payment-service/tests/test_correlation_id.py::
test_generated_request_id_survives_servicings_independent_revalidation)
hand-copied servicing's charset regex and digit ceiling into this service's
test file and checked payment-service's generated id against that COPY -- it
never ran servicing's actual code. A future edit to either service's real
validation rule (or to the copy) without updating the other would pass every
existing test while breaking span correlation in production.

This file closes that gap: it loads servicing-service's real app.main under a
private alias (both services' top-level package is named `app`, so a plain
`import app.main` here would collide with payment-service's own already-
imported `app`) and routes payment-service's outbound apply-payment call into
servicing's real TestClient, so both services' real log lines are captured
and compared directly.
"""

import importlib.util
import re
import sys
from pathlib import Path

from fastapi.testclient import TestClient

from app import payments as pay

# D19: the Idempotency-Key is required and client-minted (ADR 0013 Decision 1).
_IDEM_KEY = "11111111-1111-4111-8111-111111111111"


SERVICING_DIR = Path(__file__).resolve().parents[2] / "servicing-service"

FIELDS = re.compile(
    r"request_id=(?P<request_id>\S+) "
    r"loan_id=(?P<loan_id>\S+) "
    r"payment_id=(?P<payment_id>\S+) "
    r"outcome=(?P<outcome>\S+)"
)


def _load_servicing_main():
    """Load servicing-service's real app.main under the alias
    servicing_app_e2e. Both services' top-level package is literally named
    `app`; importing it under its own name here would either collide with or
    shadow payment-service's own already-imported `app` package."""
    alias = "servicing_app_e2e"
    if f"{alias}.main" in sys.modules:
        return sys.modules[f"{alias}.main"]
    pkg_spec = importlib.util.spec_from_file_location(
        alias,
        SERVICING_DIR / "app" / "__init__.py",
        submodule_search_locations=[str(SERVICING_DIR / "app")],
    )
    pkg = importlib.util.module_from_spec(pkg_spec)
    sys.modules[alias] = pkg
    pkg_spec.loader.exec_module(pkg)
    return importlib.import_module(f"{alias}.main")


def _wire_cross_service(monkeypatch, servicing_main):
    """Route payment-service's outbound apply-payment call into servicing's
    real TestClient instead of the network, so servicing's real handler and
    real log.info run for real -- not a stub asserting the header shape."""
    monkeypatch.setattr(pay, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(servicing_main.config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(
        servicing_main.balance, "apply_payment", lambda *a, **k: (450.0, True)
    )
    monkeypatch.setattr(pay.db, "query", lambda *a, **k: [{"id": 7}])

    class _Wrapped:
        def __init__(self, resp):
            self.status_code = resp.status_code

    def fake_post(url, json=None, headers=None, timeout=None):
        resp = TestClient(servicing_main.app).post(
            "/accounts/1/apply-payment", json=json, headers=headers
        )
        return _Wrapped(resp)

    monkeypatch.setattr(pay.httpx, "post", fake_post)


def _capture(monkeypatch, log_obj):
    lines = []

    def record(msg, *args, **kwargs):
        lines.append(msg % args if args else msg)

    monkeypatch.setattr(log_obj, "info", record)
    monkeypatch.setattr(log_obj, "error", record)
    return lines


def _ids(lines):
    return {m.group("request_id") for m in (FIELDS.search(x) for x in lines) if m}


def test_generated_id_lands_verbatim_on_servicings_real_log_line(monkeypatch):
    """V-TRACE, cross-service: POST /payments with no X-Request-Id -- the id
    payment-service mints and returns must be the SAME id servicing's real
    apply_payment handler logs, proven by actually running both services'
    code, not by comparing two copies of the validation rule."""
    servicing_main = _load_servicing_main()
    _wire_cross_service(monkeypatch, servicing_main)
    pay_lines = _capture(monkeypatch, pay.log)
    svc_lines = _capture(monkeypatch, servicing_main.log)

    result = pay.charge(
        loan_id=1,
        pan="4111111111111111",
        cvv="123",
        amount=50.0,
        idempotency_key=_IDEM_KEY,
    )

    pay_ids = _ids(pay_lines)
    svc_ids = _ids(svc_lines)
    assert pay_ids, f"payment-service logged no span line: {pay_lines}"
    assert svc_ids, f"servicing logged no span line: {svc_lines}"
    assert pay_ids == svc_ids == {result["request_id"]}, (
        f"the two halves of the span disagree: payment-service={pay_ids}, "
        f"servicing={svc_ids}, returned={result['request_id']}"
    )


def test_supplied_id_lands_verbatim_on_servicings_real_log_line(monkeypatch):
    """V-TRACE-SUPPLIED, cross-service: a caller-supplied id survives
    servicing's REAL re-validation, run for real, not asserted against a
    duplicated copy of the rule."""
    servicing_main = _load_servicing_main()
    _wire_cross_service(monkeypatch, servicing_main)
    svc_lines = _capture(monkeypatch, servicing_main.log)

    result = pay.charge(
        loan_id=1,
        pan="4111111111111111",
        cvv="123",
        amount=50.0,
        request_id="abc123",
        idempotency_key=_IDEM_KEY,
    )

    assert result["request_id"] == "abc123"
    assert _ids(svc_lines) == {"abc123"}, (
        f"supplied id did not survive servicing's real validation: {svc_lines}"
    )


def test_pii_shaped_id_never_reaches_servicings_real_log_line(monkeypatch):
    """A supplied id carrying SSN-length digits is replaced by payment-service
    before it ever reaches the wire, so servicing's real handler never even
    sees it -- proven end to end, not by asserting the header shape alone."""
    servicing_main = _load_servicing_main()
    _wire_cross_service(monkeypatch, servicing_main)
    svc_lines = _capture(monkeypatch, servicing_main.log)

    hostile = "412559981"  # bare SSN-length digit run
    result = pay.charge(
        loan_id=1,
        pan="4111111111111111",
        cvv="123",
        amount=50.0,
        request_id=hostile,
        idempotency_key=_IDEM_KEY,
    )

    assert result["request_id"] != hostile
    joined = "\n".join(svc_lines)
    assert hostile not in joined, f"PII-shaped id reached servicing's log: {svc_lines}"
    assert _ids(svc_lines) == {result["request_id"]}, (
        f"replacement id did not survive servicing's real validation: {svc_lines}"
    )
