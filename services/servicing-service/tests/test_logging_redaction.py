"""Regression: the servicing-service charge log must not leak PANs.

servicing-service exposes the legacy POST /payments charge path (app.main ->
payments.charge). It shares the byte-identical redactor with payment-service,
but the charge log is built in app.payments — so the construction-boundary fix
(mask PAN/CVV/SSN before interpolation, never log the free-text `name`) has to
live here too, not only in payment-service. These tests exercise the real
charge() path with the DB and balance apply mocked.
"""
import json
import logging
import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def temp_log_dir():
    """Temp dir for the log file; clear cached handlers to avoid cross-test bleed."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_log_dir = os.getenv("LOG_DIR")
        os.environ["LOG_DIR"] = tmpdir
        logging.getLogger("payment").handlers.clear()
        yield tmpdir
        if original_log_dir:
            os.environ["LOG_DIR"] = original_log_dir
        else:
            os.environ.pop("LOG_DIR", None)


@pytest.mark.parametrize("name", [
    "Apt 12 4111x1111x1111x1111",     # reviewer repro: leading digits defeat whole-value Luhn
    "4111x1111x1111x1111",            # bare smuggled card, letter separators
    "Unit 5 4111,1111,1111,1111",     # apartment digits + comma-separated card
    "order 99 4111 1111 1111 1111",   # order digits + spaced card
    "4111====1111====1111====1111",   # long separator runs
    "Jane Doe",                       # ordinary name — nothing to leak
])
def test_charge_log_never_contains_name(temp_log_dir, monkeypatch, name):
    """A PAN smuggled into the free-text `name` — including with LEADING ordinary
    digits (`Apt 12 ...`) that break a whole-value Luhn scrub, the specific bypass
    the reviewer demonstrated — must not reach the servicing charge log. The fix
    does not chase separators (a sliding window would false-mask ordinary IDs);
    `name` is simply not logged."""
    from app.logging_config import get_logger
    from app import payments

    logging.getLogger("payment").handlers.clear()
    monkeypatch.setattr(payments, "log", get_logger("payment"))
    monkeypatch.setattr(payments.db, "query", lambda *a, **k: [{"id": 1}])
    monkeypatch.setattr(payments.balance, "apply_payment", lambda *a, **k: 0.0)

    payments.charge(loan_id=7, pan="4111111111111111", cvv="123",
                    amount=250.0, ssn="412-55-9981", name=name)

    content = (Path(temp_log_dir) / "servicing-service.log").read_text()
    # The card the client submitted (pan field) is masked to its last 4.
    assert "411111111111" not in content, f"12-digit PAN leaked for name={name!r}"
    # name is not logged at all, so nothing smuggled into it can appear.
    assert '"name"' not in content, "name field should not be logged"
    assert "Apt" not in content and "Unit" not in content and "Jane" not in content, \
        f"raw name reached the log for {name!r}"


def test_charge_log_defeats_quote_delimiter_injection():
    """A client-controlled pan that injects a quote + field delimiter
    (`4111","x":"111111111111`) previously split the card across fake pseudo-JSON
    fields so the delimiter-sensitive formatter masked only a <13-digit fragment.
    Masking before interpolation leaves no reconstructable PAN. Exercises the real
    construction path (app.payments), not just the formatter."""
    from app import payments

    evil = '4111","x":"111111111111'
    line = "POST /payments charge req=%s -> ok" % json.dumps(
        payments._redacted_charge_req(evil, "123", "412-55-9981", 250.0, 7),
        ensure_ascii=False,
    )
    assert "411111111111" not in line, f"12-digit PAN chunk leaked: {line}"
    assert "4111" not in line, f"PAN prefix leaked: {line}"
    assert "111111111111" not in line, f"injected PAN tail leaked: {line}"
    assert "1111" in line, "last 4 of PAN should be preserved"
    assert '"123"' not in line and "412-55" not in line
    assert "9981" in line  # SSN last 4 preserved


def test_charge_log_masks_pan_cvv_ssn(temp_log_dir, monkeypatch):
    """The pan/cvv/ssn fields the charge log DOES carry are masked to last 4."""
    from app.logging_config import get_logger
    from app import payments

    logging.getLogger("payment").handlers.clear()
    monkeypatch.setattr(payments, "log", get_logger("payment"))
    monkeypatch.setattr(payments.db, "query", lambda *a, **k: [{"id": 1}])
    monkeypatch.setattr(payments.balance, "apply_payment", lambda *a, **k: 0.0)

    payments.charge(loan_id=7, pan="4111-1111-1111-1111", cvv="123",
                    amount=250.0, ssn="412-55-9981")

    content = (Path(temp_log_dir) / "servicing-service.log").read_text()
    assert "411111111111" not in content, "full PAN should be masked"
    assert "4111-1111-1111" not in content, "formatted PAN should be masked"
    assert '"123"' not in content, "CVV should be masked"
    assert "412-55" not in content, "SSN prefix should be masked"
    assert "1111" in content, "last 4 of PAN preserved"
    assert "9981" in content, "last 4 of SSN preserved"
