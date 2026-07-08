"""PII Redactor for logging.

Redacts sensitive customer data (PAN, CVV, SSN, email, phone) from text
before logging. This prevents PCI-DSS violations (plaintext PII in logs).

Redaction patterns are regex-based for performance; they handle common
formats but may not catch all variations (e.g., international phone numbers).

This file is duplicated byte-for-byte across every service (each runs in its
own container and cannot import a shared package). CI enforces that the copies
stay identical — edit here, then sync to all services (see scripts/sync_redactor).
"""
import logging
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
    def _luhn_valid(digits: str) -> bool:
        """Return True if digits pass the Luhn checksum (look like a real card)."""
        total = 0
        for i, ch in enumerate(reversed(digits)):
            d = int(ch)
            if i % 2 == 1:
                d *= 2
                if d > 9:
                    d -= 9
            total += d
        return total % 10 == 0

    @staticmethod
    def _redact_if_pan(match: re.Match) -> str:
        """Mask a candidate digit run only if it is a valid 13-19 digit card (Luhn).

        Luhn gate avoids redacting unrelated long digit runs (order IDs, timestamps).
        """
        raw = match.group(0)
        digits = re.sub(r'[ \-.]', '', raw)
        if 13 <= len(digits) <= 19 and PiiRedactor._luhn_valid(digits):
            return PiiRedactor._mask_with_last_4(digits) + ' (PAN)'
        return raw

    @staticmethod
    def redact(text: str) -> str:
        """
        Redact PII from text. Return redacted copy.

        Patterns redacted:
        - PAN (13-19 digits, Luhn-checked): Visa/MC 16, Amex 15 (378282246310005)
        - CVV: "cvv"/"cvv2"/"security_code": "123" → "cvv": "••••"
        - Full SSN: 412-55-9981 → •••-••-9981 (dashed always; bare only in a labeled field)
        - Email: user@example.com → ••••@example.com
        - Phone: 555-123-4567 → •••-•••-4567 (separated always; bare only in a labeled field)
        """
        if text is None:
            return None
        if not text:
            return text

        # 1. Redact PAN. Cards are 13-19 digits (Amex 15, Visa/MC 16, Diners 14)
        # with optional single space/hyphen separators. Candidates Luhn-checked below.
        text = re.sub(
            r'\b\d(?:[ \-]?\d){12,18}\b',
            PiiRedactor._redact_if_pan,
            text
        )
        # 1b. Dot-grouped PANs (e.g. 4111.1111.1111.1111). Kept as a separate,
        # tightly-grouped pattern so we don't mask ordinary decimals like 1234567.89.
        text = re.sub(
            r'\b\d{4}(?:\.\d{4}){2}\.\d{1,7}\b',
            PiiRedactor._redact_if_pan,
            text
        )

        # 2. Redact CVV (3-4 digits in the context of a card-security-code field).
        # Match JSON/kv patterns like "cvv": "123", cvv2=123, "security_code":"4567".
        text = re.sub(
            r'(["\']?(?:cvv2|cvv|cvc|cid|card[_ ]?security[_ ]?code|security[_ ]?code)["\']?\s*[:=]\s*["\']?)(\d{3,4})(["\']?)',
            lambda m: m.group(1) + "••••" + m.group(3),
            text,
            flags=re.IGNORECASE
        )

        # 3a. Redact dashed SSN (XXX-XX-XXXX) — unambiguous, always redact.
        # Preserve last 4 digits for audit trail.
        text = re.sub(
            r'\b\d{3}-\d{2}-(\d{4})\b',
            lambda m: '•••-••-' + m.group(1),
            text
        )
        # 3a-bis. Space-separated SSN (XXX XX XXXX). The 3-2-4 grouping is
        # SSN-specific (phones are 3-3-4), so the false-positive risk is low
        # enough to redact even unlabeled, unlike the bare-digit case in 3b.
        text = re.sub(
            r'\b\d{3} \d{2} (\d{4})\b',
            lambda m: '•••-••-' + m.group(1),
            text
        )
        # 3b. Redact bare/loosely-formatted SSN ONLY inside a labeled field, so we
        # don't mask unrelated 9-digit numbers (loan IDs, amounts, timestamps).
        text = re.sub(
            r'(["\']?(?:ssn|social[_ ]?security|tax[_ ]?id|tin)(?:[_ ]?(?:no|num|number))?s?["\']?\s*[:=]\s*["\']?)\d{3}[-\s]?\d{2}[-\s]?(\d{4})\b',
            lambda m: m.group(1) + '•••-••-' + m.group(2),
            text,
            flags=re.IGNORECASE
        )

        # 4. Redact email addresses. Redact local part, preserve domain.
        text = re.sub(
            r'\b[a-zA-Z0-9._%+\-]+@([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\b',
            lambda m: '••••@' + m.group(1),
            text
        )

        # 5a. Redact phone in a labeled field (catches bare 10-digit like
        # "phone":"5551234567" that 5b intentionally skips to avoid false positives).
        text = re.sub(
            r'(["\']?(?:phone|telephone|tel|mobile|cell|fax)(?:[_ ]?(?:no|num|number))?s?["\']?\s*[:=]\s*["\']?)\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?(\d{4})\b',
            lambda m: m.group(1) + '•••-•••-' + m.group(2),
            text,
            flags=re.IGNORECASE
        )
        # 5b. Redact free-text phone numbers (requires area code + separators to
        # avoid false positives on bare 10-digit product/order codes).
        text = re.sub(
            r'(?:\+\d{1,3}[\s.-])?(?:\(?\d{3}[\s.-]|\(?\d{3}\)[\s.-]?)\d{3}[\s.-]?(\d{4})\b',
            lambda m: '•••-•••-' + m.group(1),
            text
        )

        return text


class _RedactWrapper(logging.Formatter):
    """Wraps another formatter, redacting its output. Preserves the inner layout."""

    def __init__(self, inner: logging.Formatter):
        super().__init__()
        self._inner = inner

    def format(self, record: logging.LogRecord) -> str:
        return PiiRedactor.redact(self._inner.format(record))


def configure_uvicorn(fallback_fmt: logging.Formatter) -> None:
    """Route uvicorn's own loggers through redaction.

    uvicorn.access / uvicorn.error own their handlers and do NOT propagate to the
    app logger, so access lines (URL + query string) and startup tracebacks would
    otherwise be written unredacted. Wrap each existing formatter in place.
    """
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error"):
        lg = logging.getLogger(name)
        for h in lg.handlers:
            if isinstance(h.formatter, _RedactWrapper):
                continue
            h.setFormatter(_RedactWrapper(h.formatter or fallback_fmt))
