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
                raise ValidationFailed(f"{path or 'root'}: missing required key {key!r}")
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = [k for k in node if k not in props]
            if extra:
                raise ValidationFailed(f"{path or 'root'}: unexpected keys {extra}")
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
        raise ValidationFailed(f"{path}: {node!r} not in allowed values {schema['enum']}")


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
    """
    if not text or not text.strip():
        raise ValidationFailed("model output is empty")
    if len(text) > max_chars:
        raise ValidationFailed(f"model output too long ({len(text)} > {max_chars} chars)")
    if PiiRedactor.redact(text) != text:
        raise ValidationFailed("model output contains PII (leak guard tripped)")


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
    validate_schema(obj, schema)
    # Re-guard the decoded values: escapes that hid PII from the raw guard are
    # now unescaped. ensure_ascii=False keeps decoded chars (@, non-ASCII) intact
    # so the redactor can see them; a re-escaped dump would reintroduce the hole.
    guard_output(json.dumps(obj, ensure_ascii=False))
    return obj
