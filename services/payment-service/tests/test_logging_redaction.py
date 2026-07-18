"""Integration test: verify payment-service logs redact PII.

This test simulates a payment request with full PAN/CVV/SSN,
verifies the log file does NOT contain these fields unredacted.
"""

import io
import json
import logging
import os
import tempfile
from pathlib import Path

import pytest

from app.logging_config import get_logger, RedactingFormatter
from app.redactor import PiiRedactor


@pytest.fixture
def temp_log_dir():
    """Create a temporary directory for log files. Clears logger cache to avoid cross-test pollution."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_log_dir = os.getenv("LOG_DIR")
        os.environ["LOG_DIR"] = tmpdir
        # Clear cached loggers to prevent handlers from previous tests
        logging.getLogger("payment_test").handlers.clear()
        logging.getLogger("formatter_test").handlers.clear()
        yield tmpdir
        if original_log_dir:
            os.environ["LOG_DIR"] = original_log_dir
        else:
            os.environ.pop("LOG_DIR", None)


def test_payment_request_logging_redacts_pan(temp_log_dir):
    """Test: PAN in payment request is redacted in logs."""
    logger = get_logger("payment_test")

    # Simulate a payment request with full PAN
    payment_req = {
        "pan": "4111-1111-1111-1111",
        "cvv": "123",
        "ssn": "412-55-9981",
        "amount": 250.00,
    }
    logger.info("charge request: %s", payment_req)

    # Read the log file
    log_file = Path(temp_log_dir) / "payment-service.log"
    assert log_file.exists(), f"Log file not found at {log_file}"

    content = log_file.read_text()

    # Verify PAN first 12 digits are NOT in the log
    assert "4111-1111-1111" not in content, "Full PAN should be redacted"
    assert "4111" not in content or "411111111111" not in content, (
        "PAN prefix should be redacted"
    )

    # Verify last 4 of PAN IS in the log (preserved for reference)
    assert "1111" in content, "Last 4 of PAN should be preserved"

    # Verify CVV is redacted
    assert '"123"' not in content, "CVV should be redacted"
    assert "••••" in content, "Redaction marker should be present"


@pytest.mark.parametrize(
    "pan",
    [
        "4111/1111/1111/1111",  # slash-separated — previously leaked
        "4111_1111_1111_1111",  # underscore-separated — previously leaked
        "4111  1111  1111  1111",  # repeated whitespace — previously leaked
        "4111-1111-1111-1111",  # hyphen
        "4111 1111 1111 1111",  # single space
        "4111.1111.1111.1111",  # dotted
        "4111*1111*1111*1111",  # star — separator the field-context rule catches
        "4111|1111|1111|1111",  # pipe
        '4111"1111"1111"1111',  # double-quote — closes the quoted log field early
        "4111'1111'1111'1111",  # single-quote — same bypass, other quote char
        '4111"1111\'1111"1111',  # mixed quotes
        "4111111111111111",  # contiguous
    ],
)
def test_payment_request_logging_redacts_pan_separator_variants(temp_log_dir, pan):
    """Regression: PAN must be redacted on the charge log path for every separator
    a client can put in the unconstrained PaymentIn.pan string. Mirrors the real
    log line emitted by payments.charge (req={"pan":"...","cvv":"..."})."""
    logger = get_logger("payment_test")

    logger.info(
        'POST /payments charge req={"pan":"%s","cvv":"%s","amount":%s}',
        pan,
        "123",
        250.00,
    )

    content = (Path(temp_log_dir) / "payment-service.log").read_text()

    # The 12-digit prefix must never appear (with or without separators).
    assert "411111111111" not in content, f"raw PAN leaked for {pan!r}"
    assert pan[:14] not in content, f"formatted PAN prefix leaked for {pan!r}"
    assert "1111" in content, "last 4 of PAN should be preserved"
    assert '"123"' not in content, "CVV should be redacted"


def test_charge_log_defeats_quote_delimiter_injection():
    """Regression (charge-log construction boundary): a client-controlled pan
    that injects a quote followed by a field delimiter — `4111","x":"111111111111`
    — previously split the card number across fake pseudo-JSON fields, so the
    delimiter-sensitive formatter masked only a <13-digit fragment and the rest
    leaked. The fix masks values BEFORE interpolation, so no PAN digits survive.
    Exercises the real construction path (app.payments), not just the formatter."""
    from app import payments

    evil = '4111","x":"111111111111'  # quote + delimiter injection
    line = "POST /payments charge req=%s -> ok" % json.dumps(
        payments._redacted_charge_req(evil, "123", "412-55-9981", 250.0, 7),
        ensure_ascii=False,
    )
    # No reconstructable PAN chunk survives.
    assert "411111111111" not in line, f"12-digit PAN chunk leaked: {line}"
    assert "4111" not in line, f"PAN prefix leaked: {line}"
    assert "111111111111" not in line, f"injected PAN tail leaked: {line}"
    assert "1111" in line, "last 4 of PAN should be preserved"
    # CVV and SSN masked too.
    assert '"123"' not in line and "412-55" not in line
    assert "9981" in line  # SSN last 4 preserved


def test_name_field_exotic_separator_pan_redacted(temp_log_dir):
    """Backstop: if any OTHER path ever logs a free-text value carrying a
    separator-obfuscated PAN, the formatter still masks it. The charge path no
    longer logs `name` at all (see test_charge_log_never_contains_name), but the
    formatter defense-in-depth must remain effective."""
    logger = get_logger("payment_test")
    logger.info(
        "customer note: %s",
        json.dumps({"note": "4111*1111*1111*1111"}, ensure_ascii=False),
    )
    content = (Path(temp_log_dir) / "payment-service.log").read_text()
    assert "411111111111" not in content, "star-separated PAN leaked via free text"
    assert "4111" not in content
    assert "1111" in content


@pytest.mark.parametrize(
    "name",
    [
        "Apt 12 4111x1111x1111x1111",  # reviewer repro: leading digits defeat whole-value Luhn
        "4111x1111x1111x1111",  # bare smuggled card, letter separators
        "Unit 5 4111,1111,1111,1111",  # apartment digits + comma-separated card
        "order 99 4111 1111 1111 1111",  # order digits + spaced card
        "4111====1111====1111====1111",  # long separator runs
        "Jane Doe",  # ordinary name — nothing to leak
    ],
)
def test_charge_log_never_contains_name(temp_log_dir, monkeypatch, name):
    """Regression (Codex): a PAN smuggled into the free-text `name` — including
    with LEADING ordinary digits (`Apt 12 ...`) that break a whole-value Luhn
    scrub — must not reach the charge log. The fix does not chase separators
    (a sliding window would false-mask ordinary IDs); `name` is simply not
    logged. Exercises the real charge() path with the DB + servicing mocked."""
    from app import payments

    logging.getLogger("payment").handlers.clear()
    monkeypatch.setattr(payments, "log", get_logger("payment"))
    monkeypatch.setattr(payments.db, "query", lambda *a, **k: [{"id": 1}])
    monkeypatch.setattr(payments, "_apply_via_servicing", lambda *a, **k: None)

    payments.charge(
        loan_id=7,
        pan="4111111111111111",
        cvv="123",
        amount=250.0,
        ssn="412-55-9981",
        name=name,
    )

    content = (Path(temp_log_dir) / "payment-service.log").read_text()
    # The card the client submitted (pan field) is masked to its last 4.
    assert "411111111111" not in content, f"12-digit PAN leaked for name={name!r}"
    # name is not logged at all, so nothing smuggled into it can appear.
    assert '"name"' not in content, "name field should not be logged"
    assert "Apt" not in content and "Unit" not in content and "Jane" not in content, (
        f"raw name reached the log for {name!r}"
    )


def test_payment_request_logging_redacts_ssn(temp_log_dir):
    """Test: Full SSN in payment request is redacted; last 4 preserved."""
    logger = get_logger("payment_test")

    payment_req = {"ssn": "412-55-9981", "amount": 100.00}
    logger.info("customer: %s", payment_req)

    log_file = Path(temp_log_dir) / "payment-service.log"
    content = log_file.read_text()

    # Verify full SSN is NOT in log
    assert "412-55-9981" not in content, "Full SSN should be redacted"
    assert "412-55" not in content, "SSN prefix should be redacted"

    # Verify last 4 IS in log
    assert "9981" in content, "Last 4 of SSN should be preserved"


def test_payment_request_logging_redacts_cvv(temp_log_dir):
    """Test: CVV in payment request is redacted."""
    logger = get_logger("payment_test")

    payment_req = {"cvv": "456"}
    logger.info("payment: %s", payment_req)

    log_file = Path(temp_log_dir) / "payment-service.log"
    content = log_file.read_text()

    # Verify CVV is redacted
    assert "'cvv': '••••'" in content, "CVV value should be masked"
    assert "'cvv': '456'" not in content, "raw CVV must not appear"
    assert "••••" in content, "Redaction marker should be present"


def test_timestamp_is_not_masked_as_a_false_pan():
    # Regression (PR #7 review): the formatter redacts the MESSAGE, not the levelname/asctime
    # prefix. The previous whole-line redaction masked a Luhn-valid timestamp digit run
    # (YYYYMMDDHHMMSSmmm) as a false PAN -- corrupting the timestamp and making the CVV/PAN
    # substring assertions time-dependent (they flaked whenever the masked timestamp tail
    # happened to contain the asserted digits). A strftime format with no % directives returns
    # its literal, so this pins a Luhn-valid "timestamp" (the classic 4111... test PAN)
    # independent of the wall clock: the OLD formatter masked it, the fixed one leaves it.
    fmt = RedactingFormatter(
        "%(levelname)s %(asctime)s %(message)s", datefmt="4111111111111111"
    )
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "ok", None, None)
    out = fmt.format(rec)
    assert "4111111111111111" in out  # timestamp survives intact (not masked as a PAN)
    assert "(PAN)" not in out
    assert "ok" in out


def test_message_pan_still_masked_with_timestamp_untouched():
    # The other direction: a PAN in the MESSAGE is still masked even though the timestamp is
    # now exempt -- redaction of PII is preserved, only the timestamp scan is removed.
    fmt = RedactingFormatter("%(asctime)s %(message)s", datefmt="20000101000000")
    rec = logging.LogRecord(
        "t", logging.INFO, __file__, 1, "pan=%s", ("4111111111111111",), None
    )
    out = fmt.format(rec)
    assert "20000101000000" in out  # timestamp intact
    assert "4111111111111111" not in out  # the message PAN is masked
    assert "(PAN)" in out


def test_logging_with_email_and_phone(temp_log_dir):
    """Test: Email and phone are redacted."""
    logger = get_logger("payment_test")

    customer_data = {"email": "customer@example.com", "phone": "555-123-4567"}
    logger.info("customer: %s", customer_data)

    log_file = Path(temp_log_dir) / "payment-service.log"
    content = log_file.read_text()

    # Email local part should be redacted; domain preserved
    assert "customer@" not in content, "Email local part should be redacted"
    assert "example.com" in content, "Email domain should be preserved"

    # Phone prefix should be redacted; last 4 preserved
    assert "555-123" not in content, "Phone prefix should be redacted"
    assert "4567" in content, "Last 4 of phone should be preserved"


def test_logging_does_not_redact_non_pii(temp_log_dir):
    """Test: Non-PII data (amounts, IDs) is not redacted."""
    logger = get_logger("payment_test")

    data = {"transaction_id": "TXN-12345", "amount": "250.00", "reference": "REF-9876"}
    logger.info("transaction: %s", data)

    log_file = Path(temp_log_dir) / "payment-service.log"
    content = log_file.read_text()

    # These should NOT be redacted (not PII)
    assert "12345" in content
    assert "250.00" in content
    assert "9876" in content


def test_no_leak_via_root_handler(temp_log_dir):
    """propagate=False must stop unredacted duplication via a root handler.

    Plain (non-redacting) root handler stands in for uvicorn / basicConfig.
    If the service logger propagated, raw PAN/CVV would be formatted here from
    record.msg/args -- the leak path RedactingFormatter alone cannot close.
    """
    buffer = io.StringIO()
    root_handler = logging.StreamHandler(buffer)
    root_handler.setFormatter(logging.Formatter("%(message)s"))
    root_logger = logging.getLogger()
    root_logger.addHandler(root_handler)
    try:
        logger = get_logger("payment_test")
        logger.info("charge: %s", {"pan": "4111111111111111", "cvv": "123"})
    finally:
        root_logger.removeHandler(root_handler)

    out = buffer.getvalue()
    assert "4111111111111111" not in out, "Raw PAN leaked to root handler"
    assert '"123"' not in out, "Raw CVV leaked to root handler"
    assert logger.propagate is False, "service logger must not propagate to root"


def test_charge_masks_invalid_luhn_labeled_pan(temp_log_dir, monkeypatch):
    """An invalid-Luhn card in the labeled `pan` field is still cardholder data.

    A mistyped / test / rejected card fails the Luhn checksum but must NOT be
    logged in the clear. Exercises the real payments.charge log path (DB +
    servicing call mocked) so the label-gated masking is proven end to end, not
    just in the redactor unit.
    """
    from app import payments

    logging.getLogger("payment").handlers.clear()
    monkeypatch.setattr(payments, "log", get_logger("payment"))
    monkeypatch.setattr(payments.db, "query", lambda *a, **k: [{"id": 1}])
    monkeypatch.setattr(payments, "_apply_via_servicing", lambda *a, **k: None)

    payments.charge(
        loan_id=7,
        pan="4111111111111112",
        cvv="123",
        amount=250.00,
        ssn="412-55-9981",
        name="Jane Doe",
    )

    content = (Path(temp_log_dir) / "payment-service.log").read_text()
    assert "4111111111111112" not in content, "invalid-Luhn PAN logged in the clear"
    assert "411111111111" not in content, "PAN prefix leaked"
    assert "1112" in content, "last 4 should be preserved for reference"
    assert "(PAN)" in content, "PAN redaction marker missing"
    assert '"123"' not in content, "CVV should be redacted"
    assert "412-55-9981" not in content, "SSN should be redacted"


def test_labeled_pan_masked_regardless_of_luhn():
    """Direct redactor unit: labeled card fields mask invalid-Luhn values, while
    an UNLABELED non-Luhn digit run (order id) stays untouched."""
    r = PiiRedactor.redact
    assert "4111111111111112" not in r('{"pan":"4111111111111112"}')
    assert "1112 (PAN)" in r('{"pan":"4111111111111112"}')
    assert "1112 (PAN)" in r('{"card_number":"4111-1111-1111-1112"}')
    assert "1112 (PAN)" in r("cc_number=4111111111111112")
    # Unlabeled non-Luhn 16-digit run must NOT be masked (order id false positive).
    assert r('{"order_id":"1234567890123456"}') == '{"order_id":"1234567890123456"}'
    # A short value under a card key is left as-is (brand string, last-4).
    assert r('{"pan":"visa"}') == '{"pan":"visa"}'


def test_redacting_formatter_integration(temp_log_dir):
    """Test: RedactingFormatter is properly integrated."""
    logger = logging.getLogger("formatter_test")
    logger.handlers.clear()

    # Create a logger with RedactingFormatter
    log_file = Path(temp_log_dir) / "formatter_test.log"
    handler = logging.FileHandler(log_file)
    formatter = RedactingFormatter("%(levelname)s %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    # Log a message with PII
    logger.info("pan=4111111111111111 cvv=123 ssn=555-55-5555")

    content = log_file.read_text()

    # Verify PII is redacted
    assert "4111111111111111" not in content
    assert "123" not in content or "••••" in content
    assert "555-55" not in content
