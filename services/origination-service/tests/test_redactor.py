"""Unit tests for PiiRedactor."""
import pytest
from app.redactor import PiiRedactor


class TestPiiRedactorPan:
    """Test PAN redaction (Visa/Mastercard/Amex)."""

    def test_redact_pan_with_dashes(self):
        text = "card: 4111-1111-1111-1111"
        result = PiiRedactor.redact(text)
        assert "4111" not in result
        assert "1111" in result  # last 4 preserved
        assert "••••" in result

    def test_redact_pan_with_spaces(self):
        text = "card 4111 1111 1111 1111"
        result = PiiRedactor.redact(text)
        assert "4111" not in result or result.count("4111") == 0  # first 4111 redacted
        assert "1111" in result  # last 4 preserved

    def test_no_redact_short_number(self):
        text = "amount: 1234"
        result = PiiRedactor.redact(text)
        assert result == text  # not a PAN, should not redact

    def test_redact_amex_15_digit(self):
        text = "card: 378282246310005"
        result = PiiRedactor.redact(text)
        assert "378282246310005" not in result
        assert "37828224631" not in result
        assert "0005" in result  # last 4 preserved
        assert "•" in result

    def test_redact_amex_grouped(self):
        text = "card 3782 822463 10005"
        result = PiiRedactor.redact(text)
        assert "822463" not in result
        assert "0005" in result

    def test_no_redact_non_card_16_digits(self):
        # 16-digit run failing Luhn (order id) must NOT be redacted
        text = "order 1234567890123456"
        result = PiiRedactor.redact(text)
        assert result == text


class TestPiiRedactorCvv:
    """Test CVV redaction."""

    def test_redact_cvv_json(self):
        text = '{"cvv": "123"}'
        result = PiiRedactor.redact(text)
        assert "123" not in result
        assert "••••" in result

    def test_redact_cvv_no_quotes(self):
        text = 'cvv: 456'
        result = PiiRedactor.redact(text)
        assert "456" not in result
        assert "••••" in result

    def test_redact_cvc_variant(self):
        text = '"cvc":"789"'
        result = PiiRedactor.redact(text)
        assert "789" not in result
        assert "••••" in result


class TestPiiRedactorSsn:
    """Test SSN redaction."""

    def test_redact_ssn_with_dashes(self):
        text = "ssn: 412-55-9981"
        result = PiiRedactor.redact(text)
        assert "412-55" not in result
        assert "9981" in result  # last 4 preserved
        assert "•••-••-" in result

    def test_redact_ssn_no_dashes(self):
        text = "ssn=412559981"  # valid 9-digit SSN, no dashes
        result = PiiRedactor.redact(text)
        assert "412559" not in result
        assert "9981" in result  # last 4 preserved

    def test_redact_multiple_ssns(self):
        text = "ssn1: 111-11-1111, ssn2: 222-22-2222"
        result = PiiRedactor.redact(text)
        assert "111-11" not in result
        assert "222-22" not in result
        assert "1111" in result
        assert "2222" in result


class TestPiiRedactorEmail:
    """Test email redaction."""

    def test_redact_email_standard(self):
        text = "user@example.com"
        result = PiiRedactor.redact(text)
        assert "user" not in result
        assert "example.com" in result  # domain preserved
        assert "••••@" in result

    def test_redact_email_complex(self):
        text = "john.doe+tag@company.co.uk"
        result = PiiRedactor.redact(text)
        assert "john.doe" not in result
        assert "company.co.uk" in result
        assert "••••@" in result

    def test_redact_multiple_emails(self):
        text = "send to alice@test.com and bob@test.org"
        result = PiiRedactor.redact(text)
        assert "alice" not in result
        assert "bob" not in result
        assert "test.com" in result
        assert "test.org" in result


class TestPiiRedactorPhone:
    """Test phone redaction."""

    def test_redact_phone_with_dashes(self):
        text = "phone: 555-123-4567"
        result = PiiRedactor.redact(text)
        assert "555-123" not in result
        assert "4567" in result  # last 4 preserved
        assert "•••-•••-" in result

    def test_redact_phone_with_parens(self):
        text = "call (555) 123-4567"
        result = PiiRedactor.redact(text)
        assert "555" not in result or "555" not in result.split("(")[1] if "(" in result else True
        assert "4567" in result

    def test_bare_10_digit_not_treated_as_phone(self):
        # Deliberate: bare 10-digit runs are NOT redacted as phone, to avoid
        # false positives on product codes / IDs (see phone-regex tightening).
        text = "5551234567"
        result = PiiRedactor.redact(text)
        assert result == text


class TestPiiRedactorIntegration:
    """Integration tests: multiple PII types in one string."""

    def test_redact_payment_request(self):
        text = 'POST /charge {"pan":"4111111111111111","cvv":"123","ssn":"412-55-9981","email":"user@example.com"}'
        result = PiiRedactor.redact(text)
        # Check no sensitive data is present
        assert "4111111111111111" not in result
        assert "412-55" not in result
        assert "user@example.com" not in result or "user" not in result
        # Check last-4 and domain are preserved
        assert "1111" in result  # PAN last 4
        assert "9981" in result  # SSN last 4
        assert "example.com" in result  # email domain

    def test_redact_application_payload(self):
        text = '{"name":"John Doe","ssn":"123-45-6789","email":"john@company.com","phone":"555-987-6543"}'
        result = PiiRedactor.redact(text)
        assert "123-45" not in result
        assert "555-987" not in result
        assert "john@" not in result or "john" not in result
        assert "6789" in result  # SSN last 4
        assert "6543" in result  # phone last 4
        assert "company.com" in result  # email domain

    def test_no_false_positives(self):
        text = "amount: $10,000.00, zip: 12345, product_code: 5551"
        result = PiiRedactor.redact(text)
        # These are not PII; should be unchanged
        assert "$10,000.00" in result
        assert "12345" in result
        assert "5551" in result

    def test_empty_string(self):
        result = PiiRedactor.redact("")
        assert result == ""

    def test_none_safe(self):
        # Redactor should handle None gracefully (or raise)
        # Current implementation checks `if not text`, so it returns None as-is
        result = PiiRedactor.redact(None)
        assert result is None
