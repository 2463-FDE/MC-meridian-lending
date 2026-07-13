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
    assert scan_text("account 5551234567") == []


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
