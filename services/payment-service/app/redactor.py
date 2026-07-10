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
        r'(?:pan|card[_ ]?(?:number|no|num)|cc[_ ]?(?:number|no|num)'
        r'|credit[_ ]?card|account[_ ]?(?:number|no|num)'
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
    def _mask_bank_value(value: str) -> str:
        """Mask a labeled bank account / routing value, keeping the last 4 digits.

        Separator-agnostic (counts digits only). Used for values in a field whose
        NAME asserts it is an account/routing number, so no digit-length or Luhn
        gate — the label is the signal.
        """
        if '•' in value:
            # Already masked upstream: a card-length account_number is caught by
            # the labeled-PAN pass (rule 1a, account_* is in _PAN_KEY) and holds
            # only its audit last-4 — re-masking here would erase it.
            return value
        digits = re.sub(r'\D', '', value)
        if len(digits) < 4:
            return value  # too few digits to be an account/routing no. — leave as-is
        return PiiRedactor._mask_with_last_4(digits)

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
    def _percent_decode(s: str) -> str:
        """Single-pass percent-decode (%XX -> char) for PAN DETECTION only.

        Deliberately ONE level (no recursive re-decode): it closes the common
        encoded-separator bypass without unbounded work. Double-encoded input
        (%252D) is a documented residual. Only well-formed %XX (two hex digits)
        is decoded; a bare % or %<non-hex> is left as-is.
        """
        return re.sub(r'%([0-9A-Fa-f]{2})',
                      lambda m: chr(int(m.group(1), 16)), s)

    @staticmethod
    def _mask_pan_token(token: str) -> str:
        """Mask a PAN in one unquoted structural token (rule 1e), including a PAN
        that hides its separators — or itself — in percent-encoding.

        A URL like ?name=4111%2D1111%2D1111%2D1111 carries a PAN only once the
        %2D escapes resolve to '-'; left raw, the stray hex digits ('2') break the
        Luhn run (and a partial raw match would mangle the token). So: if a
        percent-decoded copy reveals a PAN, return the decoded+masked form — the
        token is a single field value, so replacing the encoded original wholesale
        keeps no reconstructable PAN. Otherwise fall back to the raw-token scan.

        Residuals (shared with the whole-run Luhn design, which refuses sub-window
        sliding to avoid false-masking 13-19 digit order IDs): a stray digit placed
        adjacent to the PAN in one field — a trailing partial escape ("…1111%2"), a
        PAN split across two &-separated params, or double-encoding ("%252D") —
        shifts or breaks the Luhn run and can escape. Closing these needs bounded
        sub-run Luhn with false-positive analysis, a design-level change beyond this
        pass.
        """
        decoded = PiiRedactor._percent_decode(token)
        if decoded != token:
            decoded_masked = PiiRedactor._mask_pan_in_value(decoded)
            if decoded_masked != decoded:
                return decoded_masked
        return PiiRedactor._mask_pan_in_value(token)

    # HTTP request line in an access log: METHOD <target> HTTP/x.y (target is the
    # only \S+ between the method and the protocol). Case-insensitive; works quoted
    # or unquoted. Group 3 is the request target whose query values we mask.
    _REQUEST_LINE = re.compile(
        r'(?i)\b(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)(\s+)(\S+)(\s+HTTP/\d)'
    )

    @staticmethod
    def _mask_request_target_query(m: 're.Match') -> str:
        """Mask the ENTIRE query string of an access-log request target, keeping
        only the path.

        configure_uvicorn routes uvicorn access logs — attacker-controlled request
        targets — through redact(). A client can put cardholder data anywhere in the
        query: split across param values (?pan=4111&x=111111111111), across param
        NAMES (?4111=x&111111111111=y), padded with stray digits, or percent-encoded.
        The per-field Luhn/token passes cannot catch these without cross-field
        globbing (which would false-mask unrelated numeric fields). Both names and
        values are attacker-controlled and carry no operational signal worth the PCI
        risk here — sensitive routes take PII in the POST body or integer path
        params, never the query — so the whole query is dropped to a single marker.
        The path is kept for debugging. This closes the URL-query PAN class outright
        (values AND keys) rather than chasing separator or key/value tricks.
        """
        method, sp, target, tail = m.group(1), m.group(2), m.group(3), m.group(4)
        if '?' not in target:
            return m.group(0)
        path = target.split('?', 1)[0]
        return f"{method}{sp}{path}?•••{tail}"

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
        - Bank account / routing / ABA: labeled field → ••••LAST4 (label-gated)
        - IBAN: GB82WEST12345698765432 → ••••…5432 (structure-gated, labeled or free text)
        """
        if text is None:
            return None
        if not text:
            return text

        # 0. Access-log request-target query string: drop the WHOLE query (keys and
        # values, keep only the path) BEFORE the PAN passes run. An access-log
        # request line is attacker-controlled; a client can split/pad/encode a PAN
        # across query values OR param names in ways the per-field Luhn passes below
        # cannot catch without cross-field globbing (which would false-mask unrelated
        # numeric fields). Dropping the query at the source closes that class
        # outright. Only the HTTP request target is touched — other log fields keep
        # their boundaries.
        text = PiiRedactor._REQUEST_LINE.sub(
            PiiRedactor._mask_request_target_query, text
        )

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
        # (`4111 and card 1111...`) or across `&`/`=` query boundaries. Multi-letter
        # separators in an UNQUOTED value are handled by rule 1e below (token-bounded
        # bound-free scan); a quoted value is covered by rule 1d.
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
        # 1e. PAN in UNQUOTED structured text (access-log request lines / URL query
        # strings). Rule 1b bounds a free-text separator to a single letter, and the
        # bound-free scan (1d) runs only inside quotes — so a card split by MULTI-
        # letter separators in an unquoted value (?name=4111xx1111xx1111xx1111, which
        # configure_uvicorn writes to the uvicorn access log) escaped both. Scan each
        # STRUCTURAL TOKEN — a maximal run holding no URL/log field delimiter
        # ([whitespace " ' ? & = / # ; , :]) — bound-free via _mask_pan_in_value.
        # Those delimiters bound each query value, path segment, and log field, so a
        # match cannot glob digits across fields (the reason 1b had to stay single-
        # letter); WITHIN a token any separator is allowed, closing the multi-letter
        # bypass. Luhn-gated, so unrelated long digit runs are left alone; a token
        # with <13 digits is a no-op. Each token is also percent-decoded for
        # detection (_mask_pan_token), so a PAN whose separators — or whole value —
        # are URL-encoded (?name=4111%2D1111%2D1111%2D1111) is caught too; the
        # stray hex digits in %XX would otherwise break the Luhn run.
        text = re.sub(
            r"""[^\s"'?&=/#;,:]+""",
            lambda m: PiiRedactor._mask_pan_token(m.group(0)),
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

        # 6a. Bank account / routing / ABA / IBAN in a LABELED field. These are
        # ordinary digit runs with no self-identifying shape (unlike a PAN's Luhn
        # or an SSN's 3-2-4), so they are indistinguishable from loan IDs/amounts
        # unless the field name says so — hence label-gated, like bare SSN.
        # ADR 0005: account identifiers must not leave the system; the LLM path's
        # redact_json runs each scalar through here, so this closes the account/
        # routing leak to the third-party model as well as in logs.
        #
        # The field NAME asserts the value is an account identifier, so we mask
        # the WHOLE value regardless of its internal separators or charset
        # (5551-2345, 555*1234, ACCT5551234, lowercase iban): _mask_bank_value
        # counts digits and leaves a <4-digit value (e.g. "checking") untouched,
        # so enumerating separators — a losing game, see the PAN path — is avoided.
        _BANK_KEY = (
            r'\b(?:bank[_ ]?account|account|acct|dda|ach(?:[_ ]?account)?'
            r'|routing|aba|rtn|transit|iban)(?:[_ ]?(?:number|no|num))?'
        )
        #   Quoted value: consume to the quote that TERMINATES the field.
        text = re.sub(
            r'(["\']?' + _BANK_KEY + r'["\']?\s*[:=]\s*)(["\'])(.*?)\2(?=[\s,;}\])]|$)',
            lambda m: m.group(1) + m.group(2) + PiiRedactor._mask_bank_value(m.group(3)) + m.group(2),
            text,
            flags=re.IGNORECASE
        )
        #   Unquoted value: mask up to the next delimiter.
        text = re.sub(
            r'(["\']?' + _BANK_KEY + r'["\']?\s*[:=]\s*)([^\s"\',;}\])&]+)',
            lambda m: m.group(1) + PiiRedactor._mask_bank_value(m.group(2)),
            text,
            flags=re.IGNORECASE
        )
        # 6b. IBAN in FREE TEXT (no label). Self-identifying structure (2-letter
        # country + 2 check digits + 11-30 alphanumeric). Uppercase per ISO 13616;
        # keep last 4 for reference. A labeled iban is handled by 6a above.
        text = re.sub(
            r'\b([A-Z]{2}\d{2}[A-Za-z0-9]{11,30})\b',
            lambda m: PiiRedactor._mask_with_last_4(m.group(1)),
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
