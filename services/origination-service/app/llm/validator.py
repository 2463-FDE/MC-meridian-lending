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

from ..redactor import PiiRedactor
from .errors import ValidationFailed

# Guard: reject implausibly long output (a runaway generation or an attempt to
# stuff the log). Generous — real summaries are a few hundred chars.
_MAX_OUTPUT_CHARS = 20_000


def parse_json(text: str) -> dict:
    """Parse model text as a JSON object, tolerating ```json fences.

    Raises `ValidationFailed` if it is not valid JSON or not an object.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Strip a leading ```json / ``` fence and trailing ```.
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        if cleaned.endswith("```"):
            cleaned = cleaned[: -3]
        cleaned = cleaned.strip()
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
    """Full structured path: guards, then parse, then schema-check. Returns the object."""
    guard_output(text)
    obj = parse_json(text)
    validate_schema(obj, schema)
    return obj
