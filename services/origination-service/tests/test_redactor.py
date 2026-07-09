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

    # Regression: separator variants the client can put in the unconstrained
    # PaymentIn.pan field. Slash/underscore/repeated-whitespace previously slipped
    # past the redactor and leaked the raw PAN to the charge log (PCI-DSS 3.4).
    def test_redact_pan_with_slashes(self):
        result = PiiRedactor.redact("card: 4111/1111/1111/1111")
        assert "4111/1111" not in result
        assert "411111111111" not in result
        assert "1111" in result  # last 4 preserved

    def test_redact_pan_with_underscores(self):
        result = PiiRedactor.redact("card: 4111_1111_1111_1111")
        assert "4111_1111" not in result
        assert "411111111111" not in result
        assert "1111" in result

    def test_redact_pan_with_repeated_whitespace(self):
        result = PiiRedactor.redact("card 4111  1111  1111  1111")
        assert "411111111111" not in result
        assert "1111" in result

    def test_redact_pan_labeled_field_any_separator(self):
        # A labeled card field is masked separator-agnostically — including exotic
        # separators (* | +) that a numeric-run pattern could never enumerate.
        for sep in ("/", "_", "-", " ", ".", "*", "|", "+"):
            pan = sep.join(["4111", "1111", "1111", "1111"])
            result = PiiRedactor.redact('{"pan":"%s","cvv":"123"}' % pan)
            assert "411111111111" not in result, f"PAN leaked for separator {sep!r}: {result}"
            assert "4111" not in result, f"PAN prefix leaked for separator {sep!r}: {result}"
            assert "1111" in result

    def test_redact_pan_quote_separators_in_quoted_field(self):
        # Regression: the log field is quote-delimited ({"pan":"%s"}), so a client
        # pan containing quote chars would close the field early. The labeled matcher
        # must consume past inner quotes to the field-terminating delimiter, else it
        # captures only the first segment (<13 digits) and leaks the raw PAN.
        for pan in ('4111"1111"1111"1111', "4111'1111'1111'1111", "4111\"1111'1111\"1111"):
            result = PiiRedactor.redact('{"pan":"%s","cvv":"123"}' % pan)
            assert "411111111111" not in result, f"PAN leaked for {pan!r}: {result}"
            assert result.count("4111") == 0, f"PAN prefix leaked for {pan!r}: {result}"
            assert "1111" in result
            assert '"cvv":"••••"' in result, f"trailing field mangled for {pan!r}: {result}"

    def test_redact_pan_unquoted_kv(self):
        # Unquoted key=value form, exotic separator, masked up to next delimiter.
        result = PiiRedactor.redact("pan=4111*1111*1111*1111 amount=5")
        assert "411111111111" not in result
        assert "4111*1111" not in result
        assert "amount=5" in result  # trailing field untouched

    def test_labeled_pan_non_card_value_untouched(self):
        # A non-card value in a field named 'pan' must not be mangled.
        assert PiiRedactor.redact('{"pan":"n/a"}') == '{"pan":"n/a"}'

    def test_span_field_not_treated_as_pan(self):
        # Word boundary: 'span' must not match the 'pan' key.
        text = '{"span":"1234567890123"}'
        assert PiiRedactor.redact(text) == text

    def test_free_text_pan_exotic_separators_masked(self):
        # Regression: a Luhn-valid PAN with */|/+ separators in a NON-card
        # (free-text) field — e.g. smuggled into `name` — must be caught by the
        # free-text pass, not only the labeled card-field rule. These separators
        # previously slipped, leaking the PAN into the log.
        for sep in ("*", "|", "+"):
            pan = sep.join(["4111", "1111", "1111", "1111"])
            result = PiiRedactor.redact('{"name":"%s"}' % pan)
            assert "411111111111" not in result, f"PAN leaked for sep {sep!r}: {result}"
            assert "4111" not in result, f"PAN prefix leaked for sep {sep!r}: {result}"
            assert "1111" in result  # last 4 preserved

    def test_free_text_pan_any_separator_masked(self):
        # Regression (Codex): a Luhn-valid PAN in a NON-card free-text field (e.g.
        # `name`) must be masked regardless of separator. Enumerating a separator
        # charset is a losing game — the free-text pass now allows ANY non-
        # alphanumeric separator and relies on the Luhn gate for false-positive
        # safety. comma/tilde/backslash/equals previously leaked.
        for sep in (",", "~", "\\", "=", " ", "-", "/", "_", "*", "|", "+", "."):
            pan = sep.join(["4111", "1111", "1111", "1111"])
            result = PiiRedactor.redact('{"name":"%s"}' % pan)
            assert "411111111111" not in result, f"raw PAN leaked for sep {sep!r}: {result}"
            assert "4111" not in result, f"PAN prefix leaked for sep {sep!r}: {result}"
            assert "1111" in result  # last 4 preserved

    def test_free_text_exotic_separator_non_luhn_not_masked(self):
        # The broadened separator class must stay Luhn-gated: a pipe-separated
        # short/again non-card run is left alone (no false positive).
        assert PiiRedactor.redact("cols 12|34|56|78") == "cols 12|34|56|78"
        # 16-digit run failing Luhn, star-separated, must NOT be masked.
        text = "ref 1234*5678*9012*3456"
        assert PiiRedactor.redact(text) == text

    def test_payload_value_pan_letter_and_long_separators_masked(self):
        # Regression (Codex): origination/KYC log whole request-payload dicts, so
        # a PAN hidden in a client name/address VALUE with letter separators
        # (4111x1111...) or separator runs longer than 3 (====) must be masked.
        # The bounded free-text pass (1b) misses these; the per-quoted-value scan
        # (1d) catches them regardless of separator.
        variants = [
            "4111x1111x1111x1111",              # letter separators
            "4111====1111====1111====1111",     # >3-char runs
            "card4111a1111b1111c1111end",       # embedded + alpha separators
            "4111 - / _ 1111 . 1111 ~ 1111",    # mixed multi-char
        ]
        # Both JSON (") and Python-repr (') payload shapes.
        for v in variants:
            for doc in ('{"name":"%s"}' % v, "{'name': '%s'}" % v):
                result = PiiRedactor.redact(doc)
                assert "411111111111" not in result, f"raw PAN leaked: {doc!r} -> {result!r}"
                assert "4111" not in result, f"PAN prefix leaked: {doc!r} -> {result!r}"
                assert "1111" in result  # last 4 preserved

    def test_payload_value_scan_does_not_glob_across_fields(self):
        # The per-value scan must NOT concatenate digits from separate fields into
        # a false PAN — each quoted value is scanned in isolation. Separate short
        # numeric fields stay intact.
        doc = str({"a": "12345", "b": "67890", "c": "1234", "d": "5678"})
        assert PiiRedactor.redact(doc) == doc
        # A non-Luhn 16-digit run inside one value is left alone.
        doc2 = str({"ref": "1234567890123456"})
        assert PiiRedactor.redact(doc2) == doc2

    # Access-log request-target query strings are dropped WHOLESALE (rule 0) — both
    # param names AND values — keeping only the path. A client controls the whole
    # request target, so a PAN split across values, across param NAMES, padded with
    # stray digits, letter- or percent-separated is gone before it can reach the
    # log. The per-field PAN passes (1b/1d/1e) still cover PANs OUTSIDE a request-
    # line query (payload dicts, path segments, free text); exercised elsewhere.

    def test_access_log_split_pan_across_query_values_masked(self):
        # A Luhn-valid PAN split across adjacent query VALUES
        # (?pan=4111&x=111111111111 -> 4111111111111111) is dropped with the query.
        line = "INFO GET /payments?pan=4111&x=111111111111 HTTP/1.1"
        result = PiiRedactor.redact(line)
        assert "4111111111111111" not in result
        assert "111111111111" not in result, f"split PAN leaked: {result}"
        assert result == "INFO GET /payments?••• HTTP/1.1"  # whole query gone, path kept

    def test_access_log_split_pan_across_query_keys_masked(self):
        # Regression (Codex round 5): param NAMES are attacker-controlled too, so a
        # PAN split across keys (?4111=x&111111111111=y) reconstructs from the keys
        # if only values are masked. The whole query is now dropped.
        line = "GET /payments?4111=x&111111111111=y HTTP/1.1"
        result = PiiRedactor.redact(line)
        assert "4111111111111111" not in result
        assert "111111111111" not in result, f"key-split PAN leaked: {result}"
        assert result == "GET /payments?••• HTTP/1.1"
        # Bare keys with no '=' (another key-only split form) are dropped as well.
        line2 = "GET /p?4111&111111111111 HTTP/1.1"
        assert PiiRedactor.redact(line2) == "GET /p?••• HTTP/1.1"

    def test_access_log_split_pan_with_intervening_and_path_digits_masked(self):
        # Stray digits between params (y=9) or in the path (/v1/) previously
        # defeated Luhn-based detection; dropping the whole query is immune.
        for line in (
            "INFO GET /p?pan=4111&y=9&x=111111111111 HTTP/1.1",
            "INFO GET /v1/p?pan=4111&x=111111111111 HTTP/1.1",
        ):
            result = PiiRedactor.redact(line)
            assert "4111111111111111" not in result, f"leaked: {result}"
            assert "111111111111" not in result, f"leaked: {result}"

    @pytest.mark.parametrize("value", [
        "4111x1111x1111x1111",              # single-letter separators
        "4111xx1111xx1111xx1111",           # multi-letter separators
        "4111%2D1111%2D1111%2D1111",        # percent-encoded '-'
        "%34%31%31%31%31%31%31%31%31%31%31%31%31%31%31%31",  # fully percent-encoded
    ])
    def test_access_log_query_pan_value_masked(self, value):
        # Any obfuscated PAN carried in a query value is dropped with the query.
        line = f"GET /payments?name={value} HTTP/1.1"
        result = PiiRedactor.redact(line)
        assert "4111111111111111" not in result
        assert "411111111111" not in result, f"PAN prefix leaked: {result}"
        assert value not in result, f"raw value survived: {result}"
        assert result == "GET /payments?••• HTTP/1.1"

    def test_access_log_drops_whole_query_keeps_path(self):
        # The entire query (keys + values) is replaced by a single marker; the path
        # is kept, and a query-less request line is untouched.
        assert PiiRedactor.redact("GET /applications?status=funded&limit=25 HTTP/1.1") == \
            "GET /applications?••• HTTP/1.1"
        assert PiiRedactor.redact("GET /applications HTTP/1.1") == \
            "GET /applications HTTP/1.1"


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


class TestPiiRedactorAdversarialFixes:
    """Regression tests for closed bypasses / false positives (adversarial review)."""

    def test_bare_9_digit_id_not_masked_as_ssn(self):
        # SSN masking of bare 9-digit runs used to clobber loan IDs / amounts.
        # Only DASHED or LABELED nine-digit values may be treated as SSN.
        assert PiiRedactor.redact("loan_id=402551998 approved") == "loan_id=402551998 approved"
        assert PiiRedactor.redact("principal 100000000 cents") == "principal 100000000 cents"

    def test_labeled_bare_ssn_still_masked(self):
        result = PiiRedactor.redact('{"ssn":"412559981"}')
        assert "412559981" not in result
        assert "9981" in result

    def test_cvv2_variant_masked(self):
        result = PiiRedactor.redact('{"cvv2": "123"}')
        assert "123" not in result and "••••" in result

    def test_security_code_variant_masked(self):
        result = PiiRedactor.redact('{"security_code": "456"}')
        assert "456" not in result and "••••" in result

    def test_card_security_code_variant_masked(self):
        result = PiiRedactor.redact('"card_security_code":"7890"')
        assert "7890" not in result and "••••" in result

    def test_bare_phone_in_labeled_field_masked(self):
        # JSON-serialized bodies are the common log shape; a bare 10-digit value
        # in a labeled phone field must be redacted even without separators.
        result = PiiRedactor.redact('{"phone":"5551234567"}')
        assert "5551234567" not in result
        assert "4567" in result

    def test_dotted_pan_masked(self):
        result = PiiRedactor.redact("card 4111.1111.1111.1111")
        assert "4111.1111.1111.1111" not in result
        assert "(PAN)" in result and "1111" in result

    def test_decimal_amount_not_masked_as_pan(self):
        # Dot-grouped PAN support must not swallow ordinary decimals.
        text = "amount 1234567.89 usd"
        assert PiiRedactor.redact(text) == text

    def test_pii_embedded_in_free_text_note_masked(self):
        # Review ask: free-text is where redaction most often slips. PII buried in
        # a prose note (not a labeled field) must still be masked by shape.
        # Dashed SSN — always redacted regardless of surrounding text.
        out = PiiRedactor.redact("notes: please call the borrower re SSN 412-55-9981 before noon")
        assert "412-55-9981" not in out
        assert "•••-••-9981" in out  # last-4 preserved for audit
        # PAN in a prose note.
        out = PiiRedactor.redact("notes: card on file 4111 1111 1111 1111 per client request")
        assert "4111 1111 1111 1111" not in out and "411111111111" not in out
        assert "(PAN)" in out
        # Email in a prose note — local part masked, domain kept.
        out = PiiRedactor.redact("notes: reach them at jane.doe@example.com anytime")
        assert "jane.doe@example.com" not in out
        assert "••••@example.com" in out

    def test_pii_in_free_text_json_value_masked(self):
        # Same, as a JSON free-text value (how a notes field reaches a log dump).
        import json as _json
        out = PiiRedactor.redact('{"notes": "borrower ssn 412-55-9981, ok to proceed"}')
        assert "412-55-9981" not in out
        assert _json.loads(out)  # still valid JSON
        assert "9981" in out  # last-4 preserved
