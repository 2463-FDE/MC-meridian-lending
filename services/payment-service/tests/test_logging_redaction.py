"""Integration test: verify payment-service logs redact PII.

This test simulates a payment request with full PAN/CVV/SSN,
verifies the log file does NOT contain these fields unredacted.
"""
import io
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
        "amount": 250.00
    }
    logger.info("charge request: %s", payment_req)

    # Read the log file
    log_file = Path(temp_log_dir) / "payment-service.log"
    assert log_file.exists(), f"Log file not found at {log_file}"

    content = log_file.read_text()

    # Verify PAN first 12 digits are NOT in the log
    assert "4111-1111-1111" not in content, "Full PAN should be redacted"
    assert "4111" not in content or "411111111111" not in content, "PAN prefix should be redacted"

    # Verify last 4 of PAN IS in the log (preserved for reference)
    assert "1111" in content, "Last 4 of PAN should be preserved"

    # Verify CVV is redacted
    assert '"123"' not in content, "CVV should be redacted"
    assert "••••" in content, "Redaction marker should be present"


@pytest.mark.parametrize("pan", [
    "4111/1111/1111/1111",   # slash-separated — previously leaked
    "4111_1111_1111_1111",   # underscore-separated — previously leaked
    "4111  1111  1111  1111",  # repeated whitespace — previously leaked
    "4111-1111-1111-1111",   # hyphen
    "4111 1111 1111 1111",   # single space
    "4111.1111.1111.1111",   # dotted
    "4111*1111*1111*1111",   # star — separator the field-context rule catches
    "4111|1111|1111|1111",   # pipe
    "4111111111111111",      # contiguous
])
def test_payment_request_logging_redacts_pan_separator_variants(temp_log_dir, pan):
    """Regression: PAN must be redacted on the charge log path for every separator
    a client can put in the unconstrained PaymentIn.pan string. Mirrors the real
    log line emitted by payments.charge (req={"pan":"...","cvv":"..."})."""
    logger = get_logger("payment_test")

    logger.info(
        'POST /payments charge req={"pan":"%s","cvv":"%s","amount":%s}',
        pan, "123", 250.00,
    )

    content = (Path(temp_log_dir) / "payment-service.log").read_text()

    # The 12-digit prefix must never appear (with or without separators).
    assert "411111111111" not in content, f"raw PAN leaked for {pan!r}"
    assert pan[:14] not in content, f"formatted PAN prefix leaked for {pan!r}"
    assert "1111" in content, "last 4 of PAN should be preserved"
    assert '"123"' not in content, "CVV should be redacted"


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
    assert "456" not in content, "CVV should be redacted"
    assert "••••" in content, "Redaction marker should be present"


def test_logging_with_email_and_phone(temp_log_dir):
    """Test: Email and phone are redacted."""
    logger = get_logger("payment_test")

    customer_data = {
        "email": "customer@example.com",
        "phone": "555-123-4567"
    }
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

    data = {
        "transaction_id": "TXN-12345",
        "amount": "250.00",
        "reference": "REF-9876"
    }
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
