"""Corpus hygiene gate: offline PII detection for candidate corpus files.

Regex + Luhn only — no LLM calls (ADR 0007 rule 4/5). Detection patterns mirror
the PiiRedactor on the unmerged feature/pii-redaction branch; consolidate into a
shared module once that PR merges (see Stage 1 plan, DL-5).

This module DETECTS and REFUSES; it never rewrites files. A file with any
finding is refused wholesale — exclusion over redaction (ADR 0007 rule 3).
Findings carry masked samples only; raw values must never appear in reports.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

# JSON keys that mark a record as containing sensitive identity/cardholder data,
# regardless of whether the value matches a regex (e.g. dob is just a date by value).
SENSITIVE_FIELDS = {"ssn", "pan", "cvv", "dob", "ein"}

# Field-name aliases (any record key matching this is sensitive), mirroring the
# production redactor's label sets (services/*/app/redactor.py, _PAN_KEY + SSN
# label group; consolidate per DL-5 when that branch merges). Catches
# social_security_number / tax_id / tin / card_number / account_number etc. that
# the bare SENSITIVE_FIELDS set misses.
_SENSITIVE_KEY = re.compile(
    r"^[\"']?(?:ssn|social[_ ]?security(?:[_ ]?(?:no|num|number))?"
    r"|tax[_ ]?id|tin|ein|pan|cvv2?|cvc2?|dob|date[_ ]?of[_ ]?birth"
    r"|card[_ ]?(?:number|no|num)|cc[_ ]?(?:number|no|num)|credit[_ ]?card"
    r"|(?:account|acct|bank[_ ]?account|dda|ach(?:[_ ]?account)?"
    r"|routing|aba|rtn|transit|iban)(?:[_ ]?(?:number|no|num))?"
    # Personal name / postal address. No reliable value regex (ordinary words),
    # so these are caught by field name only — like the redactor's label-gated
    # fields. The kb_dump spec lists name/address as PII and the smoke keeps them
    # out of artifacts, so a customer export or a remediated dump keeping only
    # names/addresses must still be refused.
    r"|(?:[a-z]+[_ ])?name|(?:sur|given|maiden|first|last|middle|full|f|l|m)[_ ]?name"
    r"|(?:mailing|home|billing|postal|street)[_ ]?address|addr(?:ess)?|street"
    r"|city|zip(?:[_ ]?code)?|postal[_ ]?code)"
    r"[\"']?$",
    re.I,
)

_PAN_CANDIDATE = re.compile(r"\b\d(?:[ \-]?\d){12,18}\b")
# A labeled card/account field is separator-agnostic: the label asserts a PAN, so
# underscore/slash/star separators that evade _PAN_CANDIDATE (space/hyphen only)
# are still caught. Extracted digits are Luhn-checked. Mirrors the redactor's
# labeled-PAN pass, which does not rely on separator enumeration.
_PAN_LABELED = re.compile(
    r"\b(?:pan|card[_ ]?(?:number|no|num)|cc[_ ]?(?:number|no|num)|credit[_ ]?card"
    r"|account[_ ]?(?:number|no|num)|acct[_ ]?(?:number|no|num)"
    r"|primary[_ ]?account[_ ]?number)\b\W{0,4}(\d(?:[ _\-/*.]?\d){12,24})",
    re.I,
)
# Label-gated bank/account/routing/IBAN identifiers. These are plain digit runs
# with no self-identifying shape (no Luhn, no 3-2-4), so — like a bare SSN — only
# the field label makes them detectable. Labels mirror the redactor's _BANK_KEY
# (services/*/app/redactor.py). The value must start with a digit and carry >=6
# identifier chars (or be an IBAN), so "account holder" / "account 4" don't trip.
_BANK_LABELED = re.compile(
    r"\b(?:bank[_ ]?account|account|acct|dda|ach(?:[_ ]?account)?"
    r"|routing|aba|rtn|transit|iban)(?:[_ ]?(?:number|no|num))?\b"
    r"\W{0,10}(\d[\d\-\s]{5,32}|[A-Z]{2}\d{2}[A-Za-z0-9]{11,30})",
    re.I,
)
# IBAN in free text: self-identifying (country + 2 check digits + alnum), so no
# label needed. A labeled IBAN is already covered by _BANK_LABELED.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Za-z0-9]{11,30}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# A bare 9-digit run is too ambiguous to flag, but one *labeled* is. Alias set and
# 3-2-4 optional-separator value mirror the redactor: ssn / social security /
# tax id / tin, with or without underscores and no/num/number suffixes.
_SSN_LABELED = re.compile(
    r"\b(?:ssn|social[_ ]?security|tax[_ ]?id|tin)(?:[_ ]?(?:no|num|number))?s?\b"
    r"\W{0,10}\d{3}[-\s]?\d{2}[-\s]?\d{4}\b",
    re.I,
)
_EIN = re.compile(r"\b\d{2}-\d{7}\b")
# A labeled card security code in free text. cvv is declared sensitive for JSONL
# records (SENSITIVE_FIELDS); this is the matching free-text detector so a
# markdown/note with "cvv: 123" is refused too. 3-4 digit code, label-gated to
# avoid flagging every short number. Over-refusal here just excludes a file
# (exclusion over redaction) — cheaper than leaking a card code into embeddings.
_CVV_LABELED = re.compile(
    r"\b(?:cvv2?|cvc2?|cv2|card security code|card verification(?: value)?|security code)"
    r"\b\W{0,10}(\d{3,4})\b",
    re.I,
)
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?:\(\d{3}\)\s?|\b\d{3}[ .\-])\d{3}[ .\-]\d{4}\b")
# DOB-shaped date in identity context (spec D2.1): a birth label followed
# shortly by a date-shaped value (ISO or slashed/dashed US/EU order).
_DOB_CONTEXT = re.compile(
    r"\b(?:dob|date of birth|birth ?date|born)\b\W{0,10}"
    r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{4})",
    re.I,
)


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _mask(value: str) -> str:
    """Masked sample safe for reports: last 4 chars kept for digit runs."""
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 8:
        return "•" * (len(digits) - 4) + digits[-4:]
    return "•" * len(value)


@dataclass
class Finding:
    pii_type: str
    masked_sample: str


@dataclass
class FileVerdict:
    path: str
    passed: bool
    findings: list[Finding] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for f in self.findings:
            out[f.pii_type] = out.get(f.pii_type, 0) + 1
        return out


def scan_text(text: str) -> list[Finding]:
    findings: list[Finding] = []
    ssn_spans = []
    for m in _SSN.finditer(text):
        findings.append(Finding("ssn", _mask(m.group(0))))
        ssn_spans.append(m.span())
    for m in _PAN_CANDIDATE.finditer(text):
        raw = m.group(0)
        digits = re.sub(r"[ \-]", "", raw)
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            findings.append(Finding("pan", _mask(digits)))
    for m in _PAN_LABELED.finditer(text):
        digits = re.sub(r"\D", "", m.group(1))
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            findings.append(Finding("pan", _mask(digits)))
    for m in _SSN_LABELED.finditer(text):
        # A dashed SSN is already caught by _SSN; don't double-count when the
        # labeled match just wraps that same value.
        if not any(m.start() <= s and e <= m.end() for s, e in ssn_spans):
            findings.append(Finding("ssn", _mask(m.group(0))))
            ssn_spans.append(m.span())
    for m in _DOB_CONTEXT.finditer(text):
        findings.append(Finding("dob", _mask(m.group(1))))
    for m in _CVV_LABELED.finditer(text):
        findings.append(Finding("cvv", _mask(m.group(1))))
    for m in _EIN.finditer(text):
        # An SSN match also contains a 2-7 digit shape; don't double-count.
        if not any(s <= m.start() and m.end() <= e for s, e in ssn_spans):
            findings.append(Finding("ein", _mask(m.group(0))))
    for m in _EMAIL.finditer(text):
        findings.append(Finding("email", "••••@" + m.group(0).split("@", 1)[1]))
    for m in _PHONE.finditer(text):
        if m.span() not in ssn_spans:
            findings.append(Finding("phone", _mask(m.group(0))))
    bank_spans = []
    for m in _BANK_LABELED.finditer(text):
        findings.append(Finding("bank", _mask(m.group(1))))
        bank_spans.append(m.span())
    for m in _IBAN.finditer(text):
        # A labeled IBAN is already reported by _BANK_LABELED; don't double-count.
        if not any(s <= m.start() and m.end() <= e for s, e in bank_spans):
            findings.append(Finding("bank", _mask(m.group(0))))
    return findings


def scan_record(obj: dict) -> list[Finding]:
    """Scan one structured record: sensitive field names + all string values.

    Recurses into nested dicts/lists so `{"applicant": {"ssn": ...}}` is caught
    the same as a flat record.
    """
    findings: list[Finding] = []
    for key, value in obj.items():
        if _SENSITIVE_KEY.match(key) and value not in (None, ""):
            findings.append(Finding(f"field:{key.lower()}", _mask(str(value))))
    # Scan each value separately — joining them would let the PAN pattern's
    # legal space separator fuse adjacent digit fields into one oversized run.
    # Value-level hits on already-flagged fields still add signal (they prove
    # the value is real PII, not just an unlucky field name).
    for value in obj.values():
        if isinstance(value, dict):
            findings.extend(scan_record(value))
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    findings.extend(scan_record(item))
                elif item is not None:
                    findings.extend(scan_text(str(item)))
        elif value is not None:
            findings.extend(scan_text(str(value)))
    return findings


def scan_file(path: str | Path) -> FileVerdict:
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    findings: list[Finding] = []
    if path.suffix == ".jsonl":
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                findings.extend(scan_text(line))
                continue
            if isinstance(obj, dict):
                findings.extend(scan_record(obj))
            else:
                findings.extend(scan_text(line))
    else:
        findings.extend(scan_text(text))
    return FileVerdict(path=str(path), passed=not findings, findings=findings)
