"""PII Redactor for logging.

Redacts sensitive customer data (PAN, CVV, SSN, email, phone) from text
before logging. This prevents PCI-DSS violations (plaintext PII in logs).

Redaction patterns are regex-based for performance; they handle common
formats but may not catch all variations (e.g., international phone numbers).
"""
import re


class PiiRedactor:
    """Redacts PII patterns from text. Used by logging formatters."""

    @staticmethod
    def _mask_with_last_4(text: str) -> str:
        """Mask text, preserving last 4 characters."""
        if len(text) <= 4:
            return "•" * len(text)
        return "•" * (len(text) - 4) + text[-4:]

    @staticmethod
    def redact(text: str) -> str:
        """
        Redact PII from text. Return redacted copy.

        Patterns redacted:
        - PAN (Visa/MC/Amex): 4111-1111-1111-1111 → ••••-••••-••••-1111
        - CVV: "cvv": "123" → "cvv": "••••"
        - Full SSN: 412-55-9981 → •••-••-9981
        - Email: user@example.com → ••••@•••••••.com
        - Phone: 555-123-4567 → •••-•••-4567
        """
        if not text:
            return text

        # 1. Redact PAN (Visa/Mastercard/Amex format: XXXX-XXXX-XXXX-XXXX or variations)
        # Pattern: 4+ digits, separated by - or space, repeated 4 times
        text = re.sub(
            r'\b\d{4}[\s\-]?\d{4}[\s\-]?\d{4}[\s\-]?\d{4}\b',
            lambda m: PiiRedactor._mask_with_last_4(m.group(0).replace('-', '').replace(' ', '')) + ' (PAN)',
            text
        )

        # 2. Redact CVV (3-4 digits in context of "cvv" or "cvc" field)
        # Match JSON patterns like "cvv": "123" or "cvv":"123" or cvv: 123
        text = re.sub(
            r'(["\']?(?:cvv|cvc|card_security_code)["\']?\s*[:=]\s*["\']?)(\d{3,4})(["\']?)',
            lambda m: m.group(1) + "••••" + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # 3. Redact full SSN (XXX-XX-XXXX or XXXXXXXXX format)
        # Preserve last 4 digits for audit trail
        text = re.sub(
            r'\b\d{3}[-]?\d{2}[-]?(\d{4})\b',
            lambda m: '•••-••-' + m.group(1),
            text
        )

        # 4. Redact email addresses
        # Match common email pattern; redact local part, preserve domain
        text = re.sub(
            r'\b[a-zA-Z0-9._%+\-]+@([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\b',
            lambda m: '••••@' + m.group(1),
            text
        )

        # 5. Redact phone numbers (XXX-XXX-XXXX or (XXX) XXX-XXXX or XXXXXXXXXX)
        # Preserve last 4 digits
        text = re.sub(
            r'\b(?:\(?\d{3}\)?[\s.-]?)?\d{3}[\s.-]?(\d{4})\b',
            lambda m: '•••-•••-' + m.group(1),
            text
        )

        return text
