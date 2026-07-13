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

_PAN_CANDIDATE = re.compile(r"\b\d(?:[ \-]?\d){12,18}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
# A bare 9-digit run is too ambiguous to flag, but one *labeled* as an SSN is not.
_SSN_LABELED = re.compile(
    r"\b(?:ssn|social security(?: number)?)\b\D{0,10}\d{9}\b", re.I
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
    for m in _SSN_LABELED.finditer(text):
        findings.append(Finding("ssn", _mask(m.group(0))))
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
    return findings


def scan_record(obj: dict) -> list[Finding]:
    """Scan one structured record: sensitive field names + all string values.

    Recurses into nested dicts/lists so `{"applicant": {"ssn": ...}}` is caught
    the same as a flat record.
    """
    findings: list[Finding] = []
    for key, value in obj.items():
        if key.lower() in SENSITIVE_FIELDS and value not in (None, ""):
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
