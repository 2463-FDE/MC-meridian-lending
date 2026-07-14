"""Unit tests for the corpus hygiene gate (spec D2.1–D2.3, D2.6)."""

from pathlib import Path


from rag_eval.hygiene import (
    _luhn_valid,
    _mask,
    scan_file,
    scan_record,
    scan_text,
)

REPO = Path(__file__).resolve().parents[2]


# --- PAN / Luhn ---


def test_luhn_valid_known_test_cards():
    for pan in [
        "4111111111111111",
        "5500005555555559",
        "340000000000009",
        "4012888888881881",
        "6011000990139424",
    ]:
        assert _luhn_valid(pan), pan


def test_pan_detected_16_digit():
    types = [f.pii_type for f in scan_text("card 4111111111111111 on file")]
    assert "pan" in types


def test_pan_detected_15_digit_amex():
    types = [f.pii_type for f in scan_text("amex 340000000000009 charged")]
    assert "pan" in types


def test_pan_detected_with_separators():
    types = [f.pii_type for f in scan_text("4111-1111-1111-1111")]
    assert "pan" in types


def test_luhn_invalid_digit_run_not_flagged_as_pan():
    # Card-length but fails Luhn: order id, not a PAN.
    types = [f.pii_type for f in scan_text("order 4111111111111112 shipped")]
    assert "pan" not in types


def test_unlabeled_pan_with_nonspace_separators_detected():
    # Free-text PANs with */./slash/letter separators (no card label) must flag.
    for text in (
        "4111*1111*1111*1111",
        "4111/1111/1111/1111",
        "4111.1111.1111.1111",
        "4111x1111x1111x1111",
    ):
        assert [f.pii_type for f in scan_text(text)] == ["pan"], text


def test_unlabeled_pan_separator_scan_is_luhn_gated():
    # Same shape but Luhn-invalid -> not a PAN (bounded, no false positive).
    assert "pan" not in [f.pii_type for f in scan_text("4111x1111x1111x1112")]


# --- SSN / EIN / email / phone ---


def test_ssn_detected():
    findings = scan_text("ssn 412-55-9981")
    assert [f.pii_type for f in findings] == ["ssn"]


def test_ein_detected_and_not_double_counted_as_ssn():
    findings = scan_text("ein 47-2210098")
    assert [f.pii_type for f in findings] == ["ein"]


def test_email_detected_and_masked():
    findings = scan_text("reach maria@example.com")
    assert findings[0].pii_type == "email"
    assert "maria" not in findings[0].masked_sample


def test_phone_detected():
    types = [f.pii_type for f in scan_text("call 555-123-4567")]
    assert "phone" in types


# --- masking: no raw values in report material ---


def test_masked_sample_keeps_only_last4():
    f = scan_text("4111111111111111")[0]
    assert f.masked_sample.endswith("1111")
    assert "411111" not in f.masked_sample


def test_mask_short_value_fully_hidden():
    assert set(_mask("123")) == {"•"}


# --- structured records (JSONL) ---


def test_sensitive_field_names_flagged_even_without_value_pattern():
    findings = scan_record({"dob": "1992-04-21", "income": 31000})
    assert "field:dob" in [f.pii_type for f in findings]


def test_null_sensitive_field_not_flagged():
    findings = scan_record({"pan": None, "ssn": None, "amount": 50000})
    assert findings == []


# --- clean text passes ---


def test_clean_policy_text_passes():
    text = "Approve: model score >= 660 and DTI <= 43%. Late fee $35 or 5%."
    assert scan_text(text) == []


def test_bare_10_digit_number_not_phone():
    # No sensitive label -> a bare 10-digit run is neither phone nor bank id.
    assert scan_text("order 5551234567") == []


# --- real repo files (spec D2.2, D2.3) ---


def test_kb_dump_refused():
    verdict = scan_file(REPO / "kb_dump" / "applications.jsonl")
    assert not verdict.passed
    counts = verdict.counts()
    assert counts.get("field:ssn") == 5
    assert counts.get("field:pan") == 5
    assert counts.get("field:dob") == 5
    assert counts.get("field:ein") == 1
    assert counts.get("pan") == 5  # value-level Luhn hits confirm real PANs


def test_policy_docs_pass():
    for name in ["underwriting_guidelines.md", "fee_schedule.md"]:
        verdict = scan_file(REPO / "policies" / name)
        assert verdict.passed, f"{name}: {verdict.counts()}"


def test_verdict_samples_contain_no_raw_pii():
    verdict = scan_file(REPO / "kb_dump" / "applications.jsonl")
    blob = " ".join(f.masked_sample for f in verdict.findings)
    for raw in ["330-90-5512", "4012888888881881", "412-55-9981", "1992-04-21"]:
        assert raw not in blob


# --- Teeth-review regressions (2026-07-11): free-text blind spots ---


def test_dob_in_identity_context_detected():
    assert [f.pii_type for f in scan_text("DOB: 1992-04-21")] == ["dob"]
    assert [f.pii_type for f in scan_text("date of birth 04/21/1992")] == ["dob"]
    assert [f.pii_type for f in scan_text("Born: 21-04-1992")] == ["dob"]


def test_plain_date_without_identity_context_passes():
    assert scan_text("Last reviewed: 2024-11-01. Effective 01/01/2025.") == []


def test_labeled_undashed_ssn_detected():
    findings = scan_text("applicant ssn 330905512 on file")
    assert [f.pii_type for f in findings] == ["ssn"]
    assert "330905512" not in findings[0].masked_sample


def test_bare_nine_digit_run_not_flagged():
    assert scan_text("order id 123456789 shipped") == []


def test_paren_and_space_phone_formats_detected():
    assert [f.pii_type for f in scan_text("(901) 555-1234")] == ["phone"]
    assert [f.pii_type for f in scan_text("call 901 555 1234 now")] == ["phone"]


def test_nested_record_sensitive_fields_detected():
    findings = scan_record({"applicant": {"ssn": "330905512", "dob": "1992-04-21"}})
    types = {f.pii_type for f in findings}
    assert "field:ssn" in types and "field:dob" in types


def test_nested_list_of_records_scanned():
    findings = scan_record({"applicants": [{"pan": "4111111111111111"}]})
    types = {f.pii_type for f in findings}
    assert "field:pan" in types and "pan" in types


def test_labeled_cvv_detected_in_free_text():
    # cvv is declared sensitive for JSONL; the free-text path must catch it too.
    for text in ("cvv: 123", "CVC 4567", "card security code 999", "cvv2=321"):
        findings = scan_text(text)
        assert "cvv" in [f.pii_type for f in findings], text
    # The code itself is fully masked, never echoed.
    f = scan_text("cvv: 123")[0]
    assert "123" not in f.masked_sample


def test_bare_three_digit_number_not_flagged_as_cvv():
    # No label -> no CVV finding (avoid flagging every short number).
    assert scan_text("see section 123 of the policy") == []


def test_markdown_with_cvv_label_is_refused(tmp_path):
    p = tmp_path / "note.md"
    p.write_text("# Note\n\n## Card\n\nTest card cvv: 123 for QA.\n", encoding="utf-8")
    verdict = scan_file(p)
    assert not verdict.passed
    assert "cvv" in verdict.counts()


# --- labeled aliases + separators (parity with the production redactor) ---


def test_ssn_aliases_and_underscores_detected_in_free_text():
    for text in (
        "social_security_number: 330905512",
        "social security no 330905512",
        "tax_id 330905512",
        "tin: 330-90-5512",
    ):
        assert "ssn" in [f.pii_type for f in scan_text(text)], text


def test_ssn_alias_record_key_flagged():
    findings = scan_record({"social_security_number": "330905512"})
    assert [f.pii_type for f in findings] == ["field:social_security_number"]


def test_labeled_pan_with_nonstandard_separators_detected():
    # Underscore/slash/star separators evade the bare-run PAN pattern but not the
    # labeled-card pass (separator-agnostic, Luhn-checked).
    for text in (
        "card_number: 4111_1111_1111_1111",
        "credit card 4111/1111/1111/1111",
        "acct_no 4111*1111*1111*1111",
    ):
        assert "pan" in [f.pii_type for f in scan_text(text)], text
    # A labeled field whose value is NOT card-length/Luhn is not flagged as PAN.
    assert "pan" not in [f.pii_type for f in scan_text("account number 12")]


def test_card_alias_record_key_flagged():
    types = {f.pii_type for f in scan_record({"card_number": "4111111111111111"})}
    assert "field:card_number" in types


def test_markdown_with_alias_labeled_ssn_is_refused(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(
        "# Note\n\n## Applicant\n\nsocial_security_number: 330905512 on file.\n",
        encoding="utf-8",
    )
    verdict = scan_file(p)
    assert not verdict.passed
    assert "ssn" in verdict.counts()


# --- labeled bank / routing / IBAN identifiers ---


def test_labeled_bank_and_routing_detected_in_free_text():
    for text in (
        "routing number 021000021",
        "bank account 123456789012",
        "acct_no: 4455667788",
        "ACH account 998877665544",
        "IBAN GB29NWBK60161331926819",
    ):
        assert "bank" in [f.pii_type for f in scan_text(text)], text


def test_free_text_iban_without_label_detected():
    assert "bank" in [f.pii_type for f in scan_text("wire to GB29NWBK60161331926819")]


def test_bank_label_without_identifier_not_flagged():
    # Label present but no identifier-length digit run -> not PII.
    assert scan_text("the account holder must sign the form") == []
    assert scan_text("see account 4 of the addendum") == []


def test_bank_record_key_flagged():
    types = {f.pii_type for f in scan_record({"routing_number": "021000021"})}
    assert "field:routing_number" in types


def test_markdown_with_routing_number_is_refused(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(
        "# Note\n\n## Wire\n\nSend to routing number 021000021 for settlement.\n",
        encoding="utf-8",
    )
    verdict = scan_file(p)
    assert not verdict.passed
    assert "bank" in verdict.counts()


# --- structured name / address fields (no reliable value regex, key-gated) ---


def test_name_and_address_record_fields_detected():
    findings = scan_record(
        {"name": "Alice Smith", "address": "123 Main St, Boston MA 02110"}
    )
    types = {f.pii_type for f in findings}
    assert "field:name" in types and "field:address" in types


def test_name_address_aliases_and_components_detected():
    findings = scan_record(
        {
            "applicant_name": "Bob",
            "street_address": "1 Elm",
            "city": "Boston",
            "zip": "02110",
        }
    )
    types = {f.pii_type for f in findings}
    assert {
        "field:applicant_name",
        "field:street_address",
        "field:city",
        "field:zip",
    } <= types


def test_nested_name_address_detected():
    findings = scan_record({"applicant": {"full_name": "X", "home_address": "Y"}})
    types = {f.pii_type for f in findings}
    assert "field:full_name" in types and "field:home_address" in types


def test_name_field_value_fully_masked():
    f = [x for x in scan_record({"name": "Alice Smith"}) if x.pii_type == "field:name"][
        0
    ]
    assert "Alice" not in f.masked_sample and "Smith" not in f.masked_sample


def test_jsonl_with_only_name_address_is_refused(tmp_path):
    # A remediated dump with SSN/PAN removed but names/addresses retained, or a
    # new customer export, must still be refused.
    p = tmp_path / "customers.jsonl"
    p.write_text(
        '{"name": "Alice Smith", "address": "123 Main St, Boston MA 02110"}\n',
        encoding="utf-8",
    )
    verdict = scan_file(p)
    assert not verdict.passed
    assert "field:name" in verdict.counts()


def test_free_text_labeled_name_and_address_detected():
    assert "name" in [f.pii_type for f in scan_text("applicant name: Alice Smith")]
    assert "address" in [
        f.pii_type for f in scan_text("home address: 123 Main St, Boston")
    ]
    # Value never echoed raw in the sample.
    f = [x for x in scan_text("borrower name: Alice Smith") if x.pii_type == "name"][0]
    assert "Alice" not in f.masked_sample


def test_name_address_labels_as_verbs_not_flagged():
    assert scan_text("name the beneficiary on the form") == []
    assert scan_text("address the risk described in section 3") == []
    assert scan_text("plan name: Standard") == []  # single word, too ambiguous


def test_markdown_with_labeled_name_is_refused(tmp_path):
    p = tmp_path / "note.md"
    p.write_text(
        "# Note\n\n## Applicant\n\nApplicant name: Alice Smith, approved.\n",
        encoding="utf-8",
    )
    assert not scan_file(p).passed


# --- unsupported / non-.md,.jsonl corpus files fail closed ---


def test_unsupported_extension_refused(tmp_path):
    p = tmp_path / "dump.bin"
    p.write_text("some opaque content", encoding="utf-8")
    verdict = scan_file(p)
    assert not verdict.passed
    assert "unsupported-file" in verdict.counts()


def test_csv_and_json_corpus_files_scanned(tmp_path):
    csv = tmp_path / "customers.csv"
    csv.write_text("name,ssn\nAlice,123-45-6789\n", encoding="utf-8")
    assert not scan_file(csv).passed
    js = tmp_path / "applications.json"
    js.write_text('{"ssn": "123-45-6789"}', encoding="utf-8")
    assert not scan_file(js).passed


def test_csv_scanned_structurally_by_header_not_as_blob(tmp_path):
    # Undashed SSN, plain DOB, comma-separated name/address — none regex-shaped
    # on their own; only the header binding flags them.
    p = tmp_path / "export.csv"
    p.write_text(
        "name,ssn,dob,address\nAlice Smith,330905512,1992-04-21,123 Main St\n",
        encoding="utf-8",
    )
    counts = scan_file(p).counts()
    assert {"field:name", "field:ssn", "field:dob", "field:address"} <= set(counts)


def test_tsv_routing_and_account_headers_flagged(tmp_path):
    p = tmp_path / "accts.tsv"
    p.write_text(
        "routing_number\taccount_number\n021000021\t123456789012\n", encoding="utf-8"
    )
    assert not scan_file(p).passed


def test_csv_with_only_nonsensitive_headers_passes(tmp_path):
    p = tmp_path / "rates.csv"
    p.write_text("product,rate\nLoan A,5.0\n", encoding="utf-8")
    assert scan_file(p).passed


def test_empty_file_passes_regardless_of_extension(tmp_path):
    p = tmp_path / "placeholder.dat"
    p.write_text("", encoding="utf-8")
    assert scan_file(p).passed


# --- non-UTF-8 / binary content fails closed ---


def test_utf16_markdown_with_ssn_refused(tmp_path):
    # UTF-16 hides "SSN 123-45-6789" as NUL-interleaved bytes the UTF-8 regexes
    # never match, so it must be refused rather than lossily decoded.
    p = tmp_path / "leaky.md"
    p.write_bytes("SSN 123-45-6789 on file".encode("utf-16-le"))
    verdict = scan_file(p)
    assert not verdict.passed
    assert "non-utf8-file" in verdict.counts()


def test_invalid_utf8_bytes_refused(tmp_path):
    # Latin-1 accented byte (0xE9) is not valid UTF-8 -> refuse.
    p = tmp_path / "note.md"
    p.write_bytes(b"applicant Jos\xe9 SSN 123-45-6789")
    assert not scan_file(p).passed


def test_utf16_csv_with_pii_refused(tmp_path):
    p = tmp_path / "export.csv"
    p.write_bytes("name,ssn\nAlice,330905512\n".encode("utf-16-le"))
    assert not scan_file(p).passed


def test_valid_utf8_with_accents_still_scanned(tmp_path):
    # A legitimately UTF-8 file with accents must NOT be refused as non-utf8;
    # it should scan normally (and here, flag the SSN).
    p = tmp_path / "ok.md"
    p.write_text("cliente José, SSN 123-45-6789", encoding="utf-8")
    verdict = scan_file(p)
    assert not verdict.passed
    assert "ssn" in verdict.counts()  # decoded fine, PII detected
