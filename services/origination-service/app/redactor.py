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

    # Card-number field names. Value in such a field is masked separator-agnostically
    # (see _mask_pan_value) — the field name asserts it is a PAN, so we do NOT rely on
    # digit grouping or a Luhn gate, only on the digit count. This closes the family of
    # separator bypasses (4111/1111..., 4111_1111..., 4111*1111..., etc.) on the
    # primary charge-log path where the client controls the raw pan string.
    _PAN_KEY = (
        r'(?:pan|card[_ ]?(?:number|no|num)|account[_ ]?(?:number|no|num)'
        r'|acct[_ ]?(?:number|no|num)|primary[_ ]?account[_ ]?number)'
    )

    @staticmethod
    def _mask_pan_value(value: str) -> str:
        """Mask a labeled card-field value of ANY internal format, keeping last 4 digits.

        Masks only when the value carries a card-length digit count (>=13), so a
        non-card value in a field named 'pan' (e.g. "n/a", a short token) is left
        untouched. Separators are irrelevant — only the digits are counted.
        """
        digits = re.sub(r'\D', '', value)
        if len(digits) < 13:
            return value
        return PiiRedactor._mask_with_last_4(digits) + ' (PAN)'

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
        digits = re.sub(r'\D', '', raw)
        if 13 <= len(digits) <= 19 and PiiRedactor._luhn_valid(digits):
            return PiiRedactor._mask_with_last_4(digits) + ' (PAN)'
        return raw

    @staticmethod
    def _mask_pan_in_value(value: str) -> str:
        """Mask a Luhn-valid 13-19 digit PAN hidden in ONE free-text value, with
        ANY separators between digits — letters (4111x1111...), punctuation, or
        runs longer than 3 (4111====1111...).

        Matches a maximal run of digits-plus-separators and Luhn-checks the run's
        extracted digits (via _redact_if_pan). BOUND-FREE on separators, but safe
        ONLY because redact() applies it per quoted value (step 1d), never across
        a whole log line: the quote delimits a single field, so digits from
        separate fields are never globbed into a false PAN. A <13-digit value
        skips the scan.

        Consistent with the redactor's whole-run Luhn design, this checks the
        exact digit run — it does NOT slide sub-windows (which would false-mask
        ordinary 13-19 digit numbers like order IDs). Consequence: a PAN sitting
        immediately beside OTHER digits in the same field (e.g. a house number,
        "12 <pan>") changes the run's digit count/Luhn and may escape — the known
        limitation of Luhn-run detection, not a per-field-value regression.
        """
        if sum(c.isdigit() for c in value) < 13:
            return value
        return re.sub(r'\d(?:\D*\d){12,18}', PiiRedactor._redact_if_pan, value)

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

        # 1a. PAN in a labeled card field — mask the WHOLE value, separator-agnostic.
        # PaymentIn.pan is an unconstrained string logged verbatim on the charge path,
        # so the client controls the delimiters (/, _, *, |, mixed whitespace, ...).
        # We do not enumerate separators (a losing game); we mask everything inside the
        # field value and keep only the last 4 digits. _mask_pan_value applies a digit
        # count gate so a non-card value in a 'pan' field is left alone.
        #   Quoted value: "pan":"4111*1111*1111*1111"  -> mask the whole value.
        # The value is consumed up to the closing quote that TERMINATES the field
        # (a quote followed by a field delimiter or end-of-string), NOT the first
        # quote inside the value. This closes the quote-as-separator bypass: a client
        # pan of 4111"1111"1111"1111 is logged as {"pan":"4111"1111"1111"1111",...},
        # and stopping at the first inner quote would capture only "4111" (<13 digits,
        # left unmasked). Single quotes work the same way.
        text = re.sub(
            r'(["\']?\b' + PiiRedactor._PAN_KEY + r'["\']?\s*[:=]\s*)(["\'])(.*?)\2(?=[\s,;}\])]|$)',
            lambda m: m.group(1) + m.group(2) + PiiRedactor._mask_pan_value(m.group(3)) + m.group(2),
            text,
            flags=re.IGNORECASE
        )
        #   Unquoted value: pan=4111*1111*1111*1111  -> mask up to the next delimiter.
        text = re.sub(
            r'(["\']?\b' + PiiRedactor._PAN_KEY + r'["\']?\s*[:=]\s*)([^\s"\',;}\])&]+)',
            lambda m: m.group(1) + PiiRedactor._mask_pan_value(m.group(2)),
            text,
            flags=re.IGNORECASE
        )
        # 1b. Free-text PAN. Cards are 13-19 digits (Amex 15, Visa/MC 16, Diners 14)
        # with optional separators. Between digits we allow EITHER up to 3
        # non-alphanumeric chars OR a SINGLE letter. Enumerating separators for
        # client-controlled free text (e.g. a `name` field or an access-log URL)
        # is a losing game — comma, tilde, backslash, equals, and letters each
        # reopened the leak. Candidates are Luhn-checked below, so unrelated long
        # digit runs (order IDs, timestamps) are left alone — that gate, not the
        # separator set, is what makes matching safe.
        #
        # The single-letter branch closes the unquoted access-log bypass:
        # `GET /payments?name=4111x1111x1111x1111` reaches this pass with NO
        # surrounding quotes (so quote-scoped rule 1d never fires), and a bare
        # `[^0-9A-Za-z]` class would let the letter-split card through. It is
        # deliberately bounded to ONE letter (not a run): multi-letter gaps are
        # words, and allowing them would glob digits across unrelated fields
        # (`4111 and card 1111...`) or across `&`/`=` query boundaries. A
        # two-letter obfuscation therefore still escapes here — an accepted limit
        # of bleed-safe free-text matching (rule 1d covers any separator, but only
        # inside a quoted value where the closing quote bounds the field).
        text = re.sub(
            r'\b\d(?:(?:[^0-9A-Za-z]{0,3}|[A-Za-z])\d){12,18}\b',
            PiiRedactor._redact_if_pan,
            text
        )
        # 1c. Dot-grouped PANs (e.g. 4111.1111.1111.1111). Kept as a separate,
        # tightly-grouped pattern so we don't mask ordinary decimals like 1234567.89.
        text = re.sub(
            r'\b\d{4}(?:\.\d{4}){2}\.\d{1,7}\b',
            PiiRedactor._redact_if_pan,
            text
        )
        # 1d. PAN hidden in a client-controlled free-text VALUE using separators
        # the bounded pass (1b) can't cover: letters (4111x1111x1111x1111) or
        # separator runs longer than 3 (4111====1111====...). Origination and
        # KYC log whole request-payload dicts through this formatter (str(dict) →
        # 'name': '<value>'), so a reconstructable card can hide in a name/address
        # value and reach the log. We scan WITHIN each quoted value only: inside a
        # quote every non-digit is a separator (bound-free), but the match cannot
        # cross the closing quote, so PANs are caught regardless of separator while
        # digits from other fields are never globbed together. Both ' and " (and
        # escaped inner quotes) are handled — Python repr uses ', JSON uses ".
        text = re.sub(
            r'(["\'])((?:(?!\1)[^\\]|\\.)*)\1',
            lambda m: m.group(1) + PiiRedactor._mask_pan_in_value(m.group(2)) + m.group(1),
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
        # 3b. Redact bare/loosely-formatted SSN ONLY inside a labeled field, so we
        # don't mask unrelated 9-digit numbers (loan IDs, amounts, timestamps).
        text = re.sub(
            r'(["\']?(?:ssn|social[_ ]?security(?:[_ ]?(?:no|num|number))?|tax[_ ]?id|tin)["\']?\s*[:=]\s*["\']?)\d{3}[-\s]?\d{2}[-\s]?(\d{4})\b',
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
            r'(["\']?(?:phone|telephone|tel|mobile|cell|fax)["\']?\s*[:=]\s*["\']?)\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?(\d{4})\b',
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
