"""Corpus hygiene gate: offline PII detection for candidate corpus files.

Regex + Luhn only — no LLM calls (ADR 0007 rule 4/5). Detection patterns mirror
the PiiRedactor on the unmerged feature/pii-redaction branch; consolidate into a
shared module once that PR merges (see Stage 1 plan, DL-5).

This module DETECTS and REFUSES; it never rewrites files. A file with any
finding is refused wholesale — exclusion over redaction (ADR 0007 rule 3).
Findings carry masked samples only; raw values must never appear in reports.
"""

from __future__ import annotations

import csv
import io
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
    r"^[\"']?(?:ssn(?:[_ ]?(?:no|num|number))?|social[_ ]?security(?:[_ ]?(?:no|num|number))?"
    r"|tax[_ ]?id|tin|ein|pan|cvv2?|cvc2?|dob|date[_ ]?of[_ ]?birth|birth[_ ]?date"
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

# Free-text PAN candidate: 13-19 digits with at most ONE non-digit separator
# between them (space, hyphen, slash, star, dot, a stray letter — any single
# char, but not a newline, so it never runs across lines). This mirrors the
# redactor's separator-agnostic free-text pass; Luhn (in scan_text) rejects
# ordinary long digit runs. Bounded to a single separator so it cannot greedily
# swallow surrounding text.
_PAN_CANDIDATE = re.compile(r"\b\d(?:[^0-9\n]?\d){12,18}\b")
# A labeled card/account field is also separator-agnostic; the label lets it
# catch card values even where the free-text pass's single-separator bound would
# miss (e.g. doubled separators). Extracted digits are Luhn-checked.
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
# shortly by a date-shaped value — ISO/slashed/dashed numeric, OR a month-name
# form ("Jan 2, 1980" / "2 January 1980") which is just as much a birth date.
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
_DOB_CONTEXT = re.compile(
    r"\b(?:dob|date of birth|birth ?date|born)\b\W{0,10}"
    r"(\d{4}[-/]\d{1,2}[-/]\d{1,2}"
    r"|\d{1,2}[-/]\d{1,2}[-/]\d{4}"
    r"|" + _MONTH + r"\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+" + _MONTH + r"\.?,?\s+\d{4})",
    re.I,
)
# Free-text labeled name / address (the scan_record key path handles structured
# records; this is the markdown / gold-query path). Conservative: a label,
# then ':' or '=', then a value that looks like the datum — a Title-cased name,
# or a street address that starts with a house number. Requiring the separator
# and value shape keeps "name the beneficiary" / "address the risk" from
# tripping. The value groups use (?-i:...) so the Title-case guard survives the
# re.I on the label.
_NAME_LABELED = re.compile(
    r"\b(?:full|first|last|middle|legal|given|maiden|applicant|borrower|customer)?"
    r"[ _]?name\s*[:=]\s*(?-i:([A-Z][a-z]+(?:[ '-][A-Z][a-z]+){1,3}))",
    re.I,
)
_ADDRESS_LABELED = re.compile(
    r"\b(?:home|mailing|billing|street|postal|residential)?[ _]?address\s*[:=]\s*"
    r"(\d{1,6}\s+[A-Za-z0-9][A-Za-z0-9 .,'#-]{3,60})",
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
        digits = re.sub(r"\D", "", m.group(0))
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
    for m in _NAME_LABELED.finditer(text):
        findings.append(Finding("name", _mask(m.group(1))))
    for m in _ADDRESS_LABELED.finditer(text):
        findings.append(Finding("address", _mask(m.group(1))))
    return findings


def scan_record(obj: dict) -> list[Finding]:
    """Scan one structured record: sensitive field names + all string values.

    Recurses into nested dicts/lists so `{"applicant": {"ssn": ...}}` is caught
    the same as a flat record.
    """
    findings: list[Finding] = []
    for key, value in obj.items():
        if not isinstance(key, str):
            continue
        # The key/header string is itself corpus content — a JSON key or CSV
        # header can carry raw PII (e.g. {"ssn 330-90-5512": ...} or a header row
        # that is a PAN). Scan it like any text, not just for a label match.
        findings.extend(scan_text(key))
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


# Extensions the gate knows how to read as plain free text. Anything else under a
# corpus root cannot be proven PII-free, so scan_file refuses it (fail closed) —
# a new customers.csv or a binary dump must break the gate, not slip through
# unscanned.
# NOTE: .yaml/.yml are deliberately excluded. They are structured key/value
# formats, but scan_text's name/address detectors are value-shape-gated
# (Title-case names, house-number addresses), so labeled identity fields whose
# values dodge those shapes — "name: alice smith", "address: PO Box 123",
# "city: Boston" — would pass a free-text scan clean. Rather than run scan_text
# on them (a bypass), YAML fails closed as unsupported until a structural scanner
# parses key/value records through scan_record like JSON/CSV.
_SCANNABLE_TEXT = {
    ".md",
    ".markdown",
    ".txt",
    ".text",
    ".log",
}
# CSV/TSV are scanned STRUCTURALLY (headers as keys), not as a free-text blob —
# a blob loses the header→cell binding, so a `ssn` column of undashed 9-digit
# values would pass. Delimiter picked by extension.
_DELIMITED = {".csv": ",", ".tsv": "\t"}
# A legitimate header cell is a COLUMN NAME: starts with a letter, then
# name-ish characters. A cell that starts with a digit or is a bare number/date
# (330905512, 1992-04-21) means the "header" row is really data — a headerless
# dump whose unlabeled SSN/DOB cells cannot be structurally bound, so the file
# is refused rather than trusted.
_PLAUSIBLE_HEADER = re.compile(r"[A-Za-z][\w %./()$#&-]{0,60}")
# Person-name shape: two or more Title-Case words ("Alice Smith", "Carol White").
# Used only for delimited files, to catch a name column that carries no other
# detectable signal. Over-refuses a CSV whose header/values are genuinely
# Title-Case proper nouns (rare under a corpus root) — acceptable fail-closed.
_PERSON_NAME = re.compile(r"[A-Z][a-z]+(?:[ '-][A-Z][a-z]+)+")


def _scan_json_value(value) -> list[Finding]:
    if isinstance(value, dict):
        return scan_record(value)
    if isinstance(value, list):
        out: list[Finding] = []
        for item in value:
            out.extend(_scan_json_value(item))
        return out
    if value is None:
        return []
    return scan_text(str(value))


def scan_file(path: str | Path) -> FileVerdict:
    path = Path(path)
    suffix = path.suffix.lower()
    try:
        raw = path.read_bytes()
    except OSError:
        return FileVerdict(
            str(path), False, [Finding("unreadable-file", suffix or "(none)")]
        )
    if not raw.strip():
        return FileVerdict(str(path), True)  # empty file holds no PII

    # Fail closed on non-UTF-8 / binary content. A UTF-16 or Latin-1 file can
    # hide PII from the UTF-8 regexes — "SSN 123-45-6789" as UTF-16LE is
    # NUL-interleaved (S\x00S\x00N\x00...), which no detector matches. A NUL byte
    # never appears in legitimate UTF-8 corpus text, so its presence (or a strict
    # decode failure) means we cannot prove the file clean — refuse rather than
    # lossily decode with errors="replace".
    if b"\x00" in raw:
        return FileVerdict(
            str(path), False, [Finding("non-utf8-file", suffix or "(none)")]
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return FileVerdict(
            str(path), False, [Finding("non-utf8-file", suffix or "(none)")]
        )

    findings: list[Finding] = []
    if suffix == ".jsonl":
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # A malformed line cannot be scanned structurally, and a free-text
                # fallback loses scan_record's key-aware protection — an
                # unterminated {"name":"alice smith" or {"birth_date":"..."} would
                # dodge the value-shape detectors and pass. Fail closed; don't put
                # the raw line in the sample (it is the PII we are refusing).
                findings.append(Finding("malformed-jsonl", suffix.lstrip(".")))
            else:
                findings.extend(_scan_json_value(obj))
    elif suffix == ".json":
        try:
            findings.extend(_scan_json_value(json.loads(text)))
        except json.JSONDecodeError:
            # Same reasoning as .jsonl above: no structural scan possible, so
            # refuse rather than fall back to a weaker key-blind text scan.
            findings.append(Finding("malformed-json", suffix.lstrip(".")))
    elif suffix in _DELIMITED:
        # Free-text pass over the whole file catches self-identifying PII
        # (dashed SSN, PAN, IBAN) regardless of row structure — including a
        # headerless/single-row export whose only row DictReader consumes as
        # column names, leaving zero data rows.
        findings.extend(scan_text(text))
        reader = csv.DictReader(io.StringIO(text), delimiter=_DELIMITED[suffix])
        fieldnames = reader.fieldnames or []
        rows = list(reader)
        # The header cells may themselves be data (headerless file), so scan
        # them as text too.
        for name in fieldnames:
            findings.extend(scan_text(name))
        # Scan each row like a JSON record so a sensitive header (ssn/name/dob)
        # flags its cell even when the value has no self-identifying shape.
        for row in rows:
            findings.extend(scan_record(row))
        # Zero data rows means the only row was consumed as the header — a
        # headerless dump we cannot structurally verify (and a header-only file
        # carries no corpus data anyway). Fail closed.
        if not rows:
            findings.append(Finding("no-data-rows", suffix.lstrip(".")))
        # A header cell that isn't a plausible column name (starts non-letter,
        # bare number/date) means the first row is data, not a header.
        if any(
            name.strip() and not _PLAUSIBLE_HEADER.fullmatch(name.strip())
            for name in fieldnames
        ):
            findings.append(Finding("data-shaped-header", suffix.lstrip(".")))
        # A column whose cells are person-name-shaped (Title Case, 2+ words) is a
        # name column even without a sensitive header label — bare names have no
        # other detectable shape. Two such cells (header counts) is the signal; a
        # legit "Loan Amount" header over numeric data stays under the bar.
        for col in fieldnames:
            cells = [col] + [r.get(col) for r in rows]
            nameish = sum(
                1
                for c in cells
                if isinstance(c, str) and _PERSON_NAME.fullmatch(c.strip())
            )
            if nameish >= 2:
                findings.append(Finding("name-column", suffix.lstrip(".")))
                break
    elif suffix in _SCANNABLE_TEXT:
        findings.extend(scan_text(text))
    else:
        # Unknown/unsupported extension under a corpus root — refuse rather than
        # ignore, so it fails the gate instead of bypassing it.
        return FileVerdict(
            str(path), False, [Finding("unsupported-file", suffix or "(none)")]
        )
    return FileVerdict(path=str(path), passed=not findings, findings=findings)
