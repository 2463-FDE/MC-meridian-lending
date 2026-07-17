"""Concern 6 — Validator / guardrail.

Nothing malformed is passed forward. For structured prompts the raw model text is
parsed as JSON and checked against the prompt's JSON schema; on failure the client
either falls back to a safe default or raises — never returns the bad output.
Independent of structure, every output goes through content/length/leak guards.

Schema checking is a small, dependency-free subset of JSON Schema (object /
array / string / enum / required / additionalProperties) — enough for the loan
prompts without pulling in `jsonschema`. Widen it, or swap in a real validator,
if prompts start needing more.
"""

from __future__ import annotations

import json
import re

from ..redactor import PiiRedactor
from .errors import ValidationFailed

# Guard: reject implausibly long output (a runaway generation or an attempt to
# stuff the log). Generous — real summaries are a few hundred chars.
_MAX_OUTPUT_CHARS = 20_000

# Label-only identity the shape/financial-label-based PiiRedactor cannot see: a
# person's name and a street address carry no PAN/SSN/email/phone signature, so
# "Applicant Jane Smith lives at 123 Main Street" passes the redactor untouched.
# The loan prompts forbid identity in the output; these are a fail-closed second
# gate against an upstream redaction miss, prompt injection, or provider echo.
#
# Both anchor on Title Case (real names/addresses are capitalized) so legitimate
# lowercase prose ("applicant requests $10,000 over 36 months") does not trip.
#
# A street address: a number, one-to-four Title-Case street-name words, then a
# capitalized street-type suffix.
_STREET_ADDRESS = re.compile(
    r"\b\d{1,6}\s+(?:[A-Z][A-Za-z.'-]*\s+){1,4}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|"
    r"Court|Ct|Way|Place|Pl|Terrace|Ter|Circle|Cir|Highway|Hwy|Parkway|Pkwy|"
    r"Trail|Trl|Square|Sq|Loop|Alley|Aly)\b\.?"
)
# An identity label (case-insensitive) followed by a Title-Case full name
# (>= two capitalized words), e.g. "Applicant: Jane Smith", "Borrower Jane Q Public".
_LABELED_NAME = re.compile(
    r"(?i:\b(?:applicant|borrower|co-?borrower|co-?applicant|customer|guarantor|"
    r"co-?signer|cosigner|full[ _-]?name|name)\b)[:\s]+"
    r"(?-i:[A-Z][a-z]+(?:\s+[A-Z]\.?){0,2}(?:\s+[A-Z][a-z]+)+)"
)

# Strip a leading ``` fence (optionally with a language tag, on its own line or
# inline) and a trailing ``` fence. Handles ```json\n{...}\n```, ```{...}```,
# and single-line ```json {...} ```.
_FENCE_OPEN = re.compile(r"^```[a-zA-Z0-9_-]*[ \t]*\n?")
_FENCE_CLOSE = re.compile(r"\n?```$")


def parse_json(text: str) -> dict:
    """Parse model text as a JSON object, tolerating ``` code fences.

    Raises `ValidationFailed` if it is not valid JSON or not an object.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = _FENCE_CLOSE.sub("", _FENCE_OPEN.sub("", cleaned)).strip()
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValidationFailed(f"model output is not valid JSON: {exc.msg}") from exc
    if not isinstance(obj, dict):
        raise ValidationFailed("model output JSON is not an object")
    return obj


def _check(node, schema, path: str) -> None:
    t = schema.get("type")
    if t == "object":
        if not isinstance(node, dict):
            raise ValidationFailed(f"{path or 'root'}: expected object")
        for key in schema.get("required", []):
            if key not in node:
                raise ValidationFailed(
                    f"{path or 'root'}: missing required key {key!r}"
                )
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = [k for k in node if k not in props]
            if extra:
                # Report only the COUNT and the schema-defined allowed keys — never the
                # model-controlled extra key names, which can carry application content
                # into the raised message and thence the LangSmith error field
                # (PR review). Allowed keys come from our schema, not the model.
                raise ValidationFailed(
                    f"{path or 'root'}: {len(extra)} unexpected key(s); "
                    f"allowed: {sorted(props)}"
                )
        for key, sub in props.items():
            if key in node:
                _check(node[key], sub, f"{path}.{key}" if path else key)
    elif t == "array":
        if not isinstance(node, list):
            raise ValidationFailed(f"{path}: expected array")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(node):
                _check(item, item_schema, f"{path}[{i}]")
    elif t == "string":
        if not isinstance(node, str):
            raise ValidationFailed(f"{path}: expected string")
    elif t == "number":
        if not isinstance(node, (int, float)) or isinstance(node, bool):
            raise ValidationFailed(f"{path}: expected number")
    elif t == "integer":
        if not isinstance(node, int) or isinstance(node, bool):
            raise ValidationFailed(f"{path}: expected integer")
    elif t == "boolean":
        if not isinstance(node, bool):
            raise ValidationFailed(f"{path}: expected boolean")

    if "enum" in schema and node not in schema["enum"]:
        # Do NOT interpolate `node` (the model-controlled value): a malformed enum can
        # carry non-PII application content (loan amount, income, employer, purpose) that
        # passes the leak guard but would otherwise reach the raised message and the
        # LangSmith error field (PR review). Report only the path and the schema's own
        # allowed values.
        raise ValidationFailed(f"{path}: value not in allowed enum {schema['enum']}")


def validate_schema(obj: dict, schema: dict) -> None:
    """Validate `obj` against a (subset) JSON schema. Raise `ValidationFailed`."""
    _check(obj, schema, "")


def guard_output(text: str, *, max_chars: int = _MAX_OUTPUT_CHARS) -> None:
    """Content/length/leak guards, independent of structure.

    - length: refuse output over `max_chars`.
    - content: refuse empty output.
    - leak: refuse output that still contains detectable PII (PAN/CVV/SSN/email/
      phone). The model is instructed not to emit PII; this catches regressions
      before the output is returned or logged.
    - identity: refuse label-only identity (a person name or street address) the
      shape-based redactor cannot detect. See _STREET_ADDRESS / _LABELED_NAME.
    """
    if not text or not text.strip():
        raise ValidationFailed("model output is empty")
    if len(text) > max_chars:
        raise ValidationFailed(
            f"model output too long ({len(text)} > {max_chars} chars)"
        )
    if PiiRedactor.redact(text) != text:
        raise ValidationFailed("model output contains PII (leak guard tripped)")
    if _STREET_ADDRESS.search(text):
        raise ValidationFailed(
            "model output contains a street address (identity guard)"
        )
    if _LABELED_NAME.search(text):
        raise ValidationFailed(
            "model output contains a labeled personal name (identity guard)"
        )


def validate_structured(text: str, schema: dict) -> dict:
    """Full structured path: guards, then parse, then schema-check. Returns the object.

    The leak guard runs TWICE: once on the raw text, then again on the DECODED
    object (re-serialized with ensure_ascii=False). The raw pass alone is
    bypassable — a model can escape PII so it is invisible until json.loads
    unescapes it. e.g. `maria\\u0040example.com` has no literal '@', so the email
    regex never fires on the raw string, but the decoded value is
    `maria@example.com`. Re-serializing the parsed object materializes every
    unescaped value (in a form whose quoting/`@`/`-` the redactor recognizes)
    and re-running the guard closes that bypass for email/SSN/PAN alike.
    """
    guard_output(text)
    obj = parse_json(text)
    # Re-guard the decoded values BEFORE schema validation: escapes that hid PII
    # from the raw guard are now unescaped, and schema-failure messages embed raw
    # model values (enum mismatch, unexpected keys) that propagate to logs and
    # trace sinks via the raised ValidationFailed. Guarding first means those
    # messages can only ever contain content the leak guard already passed.
    # ensure_ascii=False keeps decoded chars (@, non-ASCII) intact so the
    # redactor can see them; a re-escaped dump would reintroduce the hole.
    guard_output(json.dumps(obj, ensure_ascii=False))
    validate_schema(obj, schema)
    return obj
