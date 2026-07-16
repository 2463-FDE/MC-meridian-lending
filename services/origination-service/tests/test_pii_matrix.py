"""Adversarial PII matrix for the provider-export redactor (ADR 0005, ADR 0009).

Locks the full input surface of `request_builder.redact_json` green so PII-gate
coverage gaps are caught here — in one pytest run — instead of one-at-a-time
across serial review round-trips (see docs/teeth-review lessons from the
rag-eval hygiene gate, which took 30+ fix commits chasing PII variants one by
one). Every case below is a distinct shape/label/container an applicant field
could take; the invariant is that a seeded identifier cannot survive verbatim in
the JSON bound for the third-party model.

Companion to the targeted unit tests in test_llm_client.py — those assert
specific helper behavior; this asserts the black-box no-leak property across the
variant space as a whole.
"""

import json

import pytest

from app.llm.errors import LLMError
from app.llm.request_builder import _redact_json_var, _redacted_turn, redact_json

# Distinctive sentinels so a substring check is unambiguous.
SSN = "412559981"  # 9-digit bare
SSN_DASH = "412-55-9981"
PHONE = "2125551212"
PAN = "4111111111111111"  # Luhn-valid
DOB8 = "19700101"  # YYYYMMDD
NAME = "Zenobia"  # rare bare token
NAME2 = "Zenobia Quibblewick"
EMAIL = "zenobia@ex.com"

# (id, payload_json, forbidden_substring_in_output)
LEAK_CASES = [
    # SSN family — shaped, bare-string, bare-number, leading-zero, key spellings,
    # aliases, nested, in-array, and embedded in free text.
    ("ssn_labeled_dash", f'{{"ssn": "{SSN_DASH}"}}', "9981"),
    ("ssn_labeled_bare_str", f'{{"ssn": "{SSN}"}}', SSN),
    ("ssn_labeled_bare_num", f'{{"ssn": {SSN}}}', SSN),
    ("ssn_leadzero_num", '{"ssn": 12345678}', "12345678"),
    ("ssn_hyphen_key", f'{{"social-security-number": "{SSN}"}}', SSN),
    ("ssn_alias_no", f'{{"ssn_no": "{SSN}"}}', SSN),
    ("ssn_taxpayer_id", f'{{"taxpayer_id": {SSN}}}', SSN),
    ("ssn_itin_key", f'{{"itin": {SSN}}}', SSN),
    ("ssn_nested", f'{{"applicant": {{"ssn": "{SSN}"}}}}', SSN),
    ("ssn_in_array", f'{{"ids": [{{"ssn": "{SSN}"}}]}}', SSN),
    ("ssn_freetext_val", f'{{"note": "call {NAME} {SSN_DASH}"}}', "9981"),
    # phone — labeled number, labeled string, bare NANP number under a plain key.
    ("phone_labeled_num", f'{{"phone": {PHONE}}}', PHONE),
    ("phone_labeled_str", f'{{"phone": "{PHONE}"}}', PHONE),
    ("phone_bare_num_nonid", f'{{"contact": {PHONE}}}', PHONE),
    # DOB — YYYYMMDD number, ISO string, bare date-shaped number under a plain key.
    ("dob_yyyymmdd_num", f'{{"dob": {DOB8}}}', DOB8),
    ("dob_iso_str", '{"date_of_birth": "1970-01-01"}', "1970-01-01"),
    ("dob_bare_num_nonid", f'{{"x": {DOB8}}}', DOB8),
    # PAN — labeled/unlabeled number (Luhn), string.
    ("pan_labeled_num", f'{{"pan": {PAN}}}', PAN),
    ("pan_unlabeled_num", f'{{"x": {PAN}}}', PAN),
    ("pan_str", f'{{"card": "{PAN}"}}', PAN),
    # identity-key values — name family, employer, multi-token.
    ("name_val_bare", f'{{"name": "{NAME}"}}', NAME),
    ("first_name_val", f'{{"first_name": "{NAME}"}}', NAME),
    ("fullname_val", f'{{"fullname": "{NAME}"}}', NAME),
    ("maiden_name_val", f'{{"maiden_name": "{NAME}"}}', NAME),
    ("employer_val", f'{{"employer": "{NAME}"}}', NAME),
    ("name_multi_token", f'{{"applicant_name": "{NAME2}"}}', "Zenobia"),
    # bare name in a NON-identity key — the fail-closed string path (value side).
    ("bare_name_nonid_key", f'{{"purpose": "{NAME}"}}', NAME),
    ("bare_name_underscore", '{"purpose": "zenobia_quibblewick"}', "zenobia"),
    # address family — full address, city, zip (quasi-identifiers).
    ("address_val", f'{{"address": "{NAME2}"}}', "Zenobia"),
    ("city_val", f'{{"city": "{NAME}"}}', NAME),
    ("zip_val", '{"zip": "90210"}', "90210"),
    # PII in KEY position — email/spaced-name/dob-in-key are caller data, not labels.
    ("email_key", f'{{"{EMAIL}": 1}}', EMAIL),
    ("name_key_spaced", f'{{"{NAME2}": 1}}', "Zenobia"),
    ("dob_in_key", '{"dob 1970-01-01": 2}', "1970-01-01"),
    # email / job / company values.
    ("email_val", f'{{"email": "{EMAIL}"}}', EMAIL),
    ("job_title_val", f'{{"job_title": "{NAME}"}}', NAME),
    ("company_val", f'{{"company": "{NAME}"}}', NAME),
]


@pytest.mark.parametrize(
    "payload,forbidden",
    [(p, f) for _, p, f in LEAK_CASES],
    ids=[cid for cid, _, _ in LEAK_CASES],
)
def test_no_pii_survives_redact_json(payload, forbidden):
    out = redact_json(payload)
    assert forbidden not in out, f"leaked {forbidden!r} in {out!r}"
    # Output must stay JSON-valid — a malformed prompt would break the model read.
    json.loads(out)


@pytest.mark.xfail(
    reason="Documented, accepted residual (request_builder._is_field_name): a "
    "no-separator bare name used as an object KEY is byte-identical to a schema "
    "field name and cannot be masked on shape without destroying legit keys. "
    "Accepted because keys are system-defined tokens, not caller free-text.",
    strict=True,
)
def test_documented_residual_bare_name_key():
    # If this ever PASSES (redactor learned to mask bare-name keys), delete the
    # xfail — the residual is closed.
    assert "Zenobia" not in redact_json('{"Zenobia": 1}')


@pytest.mark.parametrize(
    "value",
    [
        '"Zenobia DOB 1970-01-01"',  # bare scalar — no keys to mask identity from
        '[{"ssn": "412-55-9981"}]',  # top-level array — same
        "Zenobia 412-55-9981",  # not JSON at all
    ],
    ids=["bare_scalar", "top_array", "non_json"],
)
def test_json_var_refuses_unmaskable_shapes(value):
    # A declared JSON variable that is not an object cannot have label-only
    # identity masked structurally — it must REFUSE, never fall back and leak.
    with pytest.raises(LLMError):
        _redact_json_var("application", value)


@pytest.mark.parametrize(
    "turn",
    [
        {"role": "user", "content": "Zenobia 412-55-9981"},  # free-text content
        {"role": "Zenobia", "content": '{"a": 1}'},  # unrecognized role
    ],
    ids=["non_object_content", "bad_role"],
)
def test_history_turn_refuses_unmaskable(turn):
    with pytest.raises(LLMError):
        _redacted_turn(turn)
