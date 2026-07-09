"""Concern 3 — Request builder.

Assembles the full prompt from parts — system + few-shot examples + prior
conversation history + the current user message — pulling the template from the
prompt library rather than inlining strings. Enforces a token budget: it counts
tokens, reserves room for the answer, and trims the *oldest* history turns first
to make the request fit. If even the irreducible parts (system + examples +
current message + reserved answer) exceed the budget, it refuses up front so an
oversized request never reaches the network.

Token counting is a heuristic (~4 characters per token, per ADR 0005). It is an
estimate used for budgeting only — deliberately conservative so we refuse early
rather than overshoot. Swap in a real tokenizer later without changing callers.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from ..prompts import PromptTemplate
from ..redactor import PiiRedactor
from .adapter import CompletionRequest
from .errors import LLMError, TokenBudgetExceeded

_CHARS_PER_TOKEN = 4

# Value substituted for a direct-identity field before the application reaches
# the provider. Uses the redactor's • glyph so it reads as a redaction, not data.
_IDENTITY_MASK = "•••• (redacted)"

# Value substituted for an applicant-controlled FREE-TEXT field (e.g. `purpose`).
# A string under a non-identity key is unstructured applicant text if it holds
# whitespace OR is not a strict operational-code token (see _SAFE_TOKEN): it can
# embed a name/DOB/address the pattern redactor cannot detect (no label, no shape),
# so it must not reach the provider raw. FAIL-CLOSED — a string is sent raw only
# when it is a single safe-token; everything else is masked (see _redact_scalar).
_FREETEXT_MASK = "•••• (free text redacted)"

# The ONLY string shape allowed to reach the provider raw under a non-identity key.
# A lowercase snake/alnum token (auto, debt_consolidation, term36) is an operational
# enum/code that carries no identity. Whitespace alone is NOT a sufficient boundary
# between safe codes and unsafe free text — a hyphenated/concatenated name
# (Jane-Smith, JaneSmith) and an ISO date (1970-01-01) have no whitespace yet are
# identity, and the pattern redactor (name/date have no shape) would let them
# through. Requiring all-lowercase, no separators but underscore, rejects those:
# caps (names), hyphens/colons (dates), and dots/at-signs (emails) all fail the
# match. Residual (inherent to any shape rule, documented): a deliberately
# lowercased bare name like "jane" is indistinguishable from a code and still
# passes — closing that needs the field-level allowlist noted in _require_json_object.
_SAFE_TOKEN = re.compile(r'[a-z0-9][a-z0-9_]*')


def _is_identity_key(key) -> bool:
    """True if a field NAME denotes a direct applicant identifier.

    The pattern redactor (PiiRedactor) masks values with a self-identifying shape
    — PAN (Luhn), SSN (3-2-4), email, phone, bank/IBAN. Name, date of birth,
    address, EIN and employer have NO such shape: only the field label identifies
    them, so they slip through the pattern pass and would reach the third-party
    model raw. ADR 0005 least-privilege: these nonessential identifiers must not
    leave the trust boundary. This is label-gated (like bare SSN / bank in the
    redactor) — the label is the only available signal.
    """
    k = str(key).strip().lower()
    # Normalize separators so concatenated and separated spellings match the same
    # rules: firstname == first_name == first-name.
    kn = k.replace("_", "").replace("-", "").replace(" ", "")
    # name family: name, first/last/middle/full/maiden/sur-name — separated OR
    # concatenated (firstname, surname, fullname), and employer_name/company_name.
    if kn == "name" or kn.endswith("name") or kn == "surname":
        return True
    if kn in {"firstname", "lastname", "middlename", "fullname", "maidenname"}:
        return True
    # date of birth: dob, date_of_birth, birthdate, dateofbirth
    if kn == "dob" or "birth" in kn:
        return True
    # postal address family (city/zip alone are quasi-identifiers; mask them too)
    if "address" in kn or kn.startswith(("street", "zip", "postal", "postcode")) \
            or kn == "city":
        return True
    # employer identification: ein (any prefix: federal_ein, employer_ein), employer*,
    # company; and job title (employment identity, like employer).
    if kn == "ein" or kn.endswith("ein") or kn.startswith("employer") \
            or kn == "company" or "jobtitle" in kn or kn == "title":
        return True
    return False


def _redact_scalar(key: str, value):
    """Redact one JSON scalar while keeping the result JSON-valid.

    Two-stage guard on a string value under a non-identity key:

    1. WHITESPACE ⇒ multi-token free text ⇒ masked WHOLESALE. Such a value can
       carry label-less identity around any shaped PII (`"call Jane Smith
       412-55-9981"`); pattern-masking only the SSN would leak "Jane Smith", so
       the whole value is dropped.
    2. NO WHITESPACE ⇒ a single token. It is redacted *with its field label*
       (`"key": value`) so the label-gated / shape patterns fire, keeping the
       audit last-4 of a genuine single shaped token (an SSN/PAN/email is a token,
       not free text). If nothing shaped matched, the token is passed raw ONLY
       when it is a strict operational-code token (`_SAFE_TOKEN`); a name
       (Jane-Smith, JaneSmith) or a date (1970-01-01) has no whitespace and no
       shape, so it FAILS the token test and is masked. Numbers and other
       non-string scalars skip step 1 and go straight to the pattern pass so a
       bare SSN/PAN literal is still caught. Empty string carries nothing, kept.

    Residual (documented, closed only by the field allowlist in
    `_require_json_object`): a no-whitespace value that dash/underscore-joins a
    name to shaped PII (`"Jane-Smith-412-55-9981"`) keeps the name after the SSN
    is masked. Real free text has whitespace and is caught by step 1.
    """
    if isinstance(value, bool) or value is None:
        return value
    # Step 1 — whitespace-bearing strings are free text: mask wholesale.
    if isinstance(value, str) and any(ch.isspace() for ch in value):
        return _FREETEXT_MASK
    # Step 2 — single token (or a number): label-gated pattern pass keeps a
    # genuine shaped token's audit last-4.
    prefix = f'"{key}": '
    probe = prefix + json.dumps(value)
    redacted = PiiRedactor.redact(probe)
    if redacted != probe:
        if not redacted.startswith(prefix):
            # Redaction altered the key/prefix (a PII-shaped field name); the
            # value slice would be misaligned. Fall back to redacting the value
            # alone — loses the label hint but stays correct and JSON-valid.
            return PiiRedactor.redact(value if isinstance(value, str) else json.dumps(value))
        masked = redacted[len(prefix):]
        if len(masked) >= 2 and masked[0] == '"' and masked[-1] == '"':
            masked = masked[1:-1]  # drop the quotes json.dumps added; re-quoted on output
        return masked
    # Nothing shaped. Fail closed: a non-empty string that is not a strict
    # operational-code token can hide label-less identity, so mask it.
    if isinstance(value, str) and value and not _SAFE_TOKEN.fullmatch(value):
        return _FREETEXT_MASK
    return value  # numbers stay numbers; recognized code tokens (and "") pass raw


def _redact_key(key) -> str:
    """Redact PII out of an object key. Keys are normally field names, but a
    caller-supplied document could carry customer data in a key
    (`{"contact@ex.com": ...}`); that key would otherwise reach the model raw.
    """
    return PiiRedactor.redact(str(key))


def _redact_node(node, key: str = ""):
    if isinstance(node, dict):
        # Redact both the key (PII-in-key leak) and the value; the value is
        # redacted with the ORIGINAL key as its label so label-gated patterns
        # still fire before the key itself is masked. A direct-identity field
        # (name/DOB/address/EIN/employer) is generalized to a mask wholesale —
        # the pattern redactor cannot catch these (no self-identifying shape),
        # so the label is the gate and the entire subtree is dropped.
        return {
            _redact_key(k): (_IDENTITY_MASK if _is_identity_key(k) else _redact_node(v, k))
            for k, v in node.items()
        }
    if isinstance(node, list):
        return [_redact_node(v, key) for v in node]
    return _redact_scalar(key, node)


def redact_json(json_str: str) -> str:
    """Redact PII from a JSON document without breaking its syntax.

    Whole-string redaction (used for logs and free text) can turn a bare numeric
    PII literal — an SSN or PAN encoded as a JSON *number* — into unquoted mask
    text (`"ssn": •••-••-9981`), producing invalid JSON inside a prompt that asks
    the model to read Application (JSON). This parses the document, redacts each
    scalar in the context of its field label, and re-serializes, so masked values
    stay valid JSON strings.

    Direct-identity fields (name, DOB, address, EIN, employer) carry no
    self-identifying shape for the pattern redactor to key on, so they are
    generalized to a mask by field label (see `_is_identity_key`) — least
    privilege: they are nonessential to a triage summary and must not leave the
    trust boundary.

    Falls back to whole-string `PiiRedactor.redact` when the input is not valid
    JSON — never weaker than the prior behavior.
    """
    try:
        data = json.loads(json_str)
    except (ValueError, TypeError):
        return PiiRedactor.redact(json_str)
    # ensure_ascii=False keeps mask glyphs (•) literal, matching the whole-string
    # redactor's output rather than emitting • escapes.
    return json.dumps(_redact_node(data), ensure_ascii=False)


def _require_json_object(value: str):
    """Parse `value` and require it to be a JSON OBJECT, else raise LLMError.

    A JSON *object* is the only shape in which label-only identifiers
    (name/DOB/address/employer) can be masked — masking keys on the identity
    label. A bare JSON scalar ("Jane Smith DOB 1970-01-01") or array parses fine
    but has no keys, so it would be redacted as prose and ship identity raw; that
    is the same leak as malformed input, just wrapped in quotes/brackets. So both
    non-JSON and non-object JSON fail closed.

    Known residual (inherent, documented): an object still masks only VALUES
    under recognized identity KEYS. A name a caller stuffs into a free-text value
    under a non-identity key (e.g. {"notes": "Applicant Jane Smith"}) has no label
    and no shape, so — like the redactor's free-text limitation — it is not
    masked. Callers must not place raw applicant identity in free-text fields.
    """
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else False


def _redact_json_var(name: str, value: str) -> str:
    """Redact a declared JSON variable, failing closed unless it is a JSON object.

    A `json_vars` field (e.g. application_json) is contractually a JSON object,
    the only shape in which label-only identifiers can be masked structurally. A
    value that does not parse, or parses to a non-object, would fall through to
    weaker redaction and ship identity raw, so we REFUSE before the network — a
    caller bug, schema drift, or a half-serialized payload must never export
    applicant identity to the third-party model (ADR 0005 least-privilege).
    """
    if _require_json_object(value) in (None, False):
        raise LLMError(
            f"{name} must be a JSON object: it is a declared identity-bearing "
            f"document and label-only identifiers (name/DOB/address/employer) "
            f"can only be masked structurally. Refusing to send a "
            f"partially-redacted payload (invalid JSON or a bare scalar/array is "
            f"treated as prose) to the provider."
        )
    return redact_json(value)


def _redacted_turn(turn: dict) -> dict:
    """Validate a caller-supplied history turn and redact its content.

    History is arbitrary caller text and may replay raw application data, so it
    needs the SAME structural identity masking as the current message. Label-only
    identifiers (name/DOB/address/employer) have no shape the pattern redactor
    can catch, and they cannot be reliably scrubbed from free prose (an unlabeled
    name is indistinguishable from ordinary text). So history FAILS CLOSED: each
    turn's content must be a JSON OBJECT, which `redact_json` then masks;
    free-form prose — or a bare JSON scalar/array, which is just prose in
    quotes/brackets — is refused rather than leaked (ADR 0005 least-privilege,
    same stance as the gated stream() path). See `_require_json_object` for the
    residual on identity placed in free-text object values.

    Raises `LLMError` on a malformed turn (missing/non-string content, or content
    that is not a JSON object) instead of a bare KeyError, so callers get a typed
    failure.
    """
    if not isinstance(turn, dict) or "content" not in turn:
        raise LLMError("history turn must be a dict with a 'content' key")
    content = turn["content"]
    if not isinstance(content, str):
        raise LLMError("history turn 'content' must be a string")
    if _require_json_object(content) in (None, False):
        raise LLMError(
            "history turn 'content' must be a JSON object so applicant identity "
            "can be masked before it reaches the provider; free-form text (or a "
            "bare JSON scalar/array, which is just prose in quotes) is refused "
            "because label-only identifiers (name/DOB/address/employer) cannot "
            "be reliably scrubbed from prose. Pass the prior turn as a JSON object."
        )
    return {"role": turn.get("role", "user"), "content": redact_json(content)}


def estimate_tokens(text: str) -> int:
    """Rough token estimate for budgeting. Conservative (rounds up)."""
    if not text:
        return 0
    return -(-len(text) // _CHARS_PER_TOKEN)  # ceil division


@dataclass
class BuiltRequest:
    """A CompletionRequest plus the estimate used to admit it (for logging)."""

    request: CompletionRequest
    estimated_input_tokens: int
    trimmed_history_turns: int


def _expand_examples(examples: list) -> list[dict]:
    """Turn few-shot pairs into alternating user/assistant messages."""
    out: list[dict] = []
    for ex in examples:
        out.append({"role": "user", "content": ex["user"]})
        out.append({"role": "assistant", "content": ex["assistant"]})
    return out


def build_request(
    template: PromptTemplate,
    *,
    model: str,
    max_tokens: int,
    temperature: float,
    timeout: float,
    token_budget: int,
    history: list[dict] | None = None,
    idempotency_key: str = "",
    **variables,
) -> BuiltRequest:
    """Assemble a `CompletionRequest` within the token budget.

    Order of assembly: system (from template) + few-shot examples + trimmed
    history + current user message (rendered from template).

    Customer PII in the current message and in history is redacted BEFORE the
    request is built (ADR 0005 decision #2 — least privilege: raw PII must not
    leave the system to a third-party provider). Declared `json_vars` and history
    turns are masked JSON-aware and FAIL CLOSED on invalid JSON (see
    _redact_json_var / _redacted_turn), so label-only identifiers cannot escape
    via a malformed payload or free-form prose. System and few-shot examples are
    authored by us and carry no customer PII.

    Raises `TokenBudgetExceeded` if the non-trimmable parts plus the reserved
    answer room do not fit in `token_budget`. Raises `LLMError` on a malformed
    history turn, a history turn that is not valid JSON, or a declared JSON
    variable that is not valid JSON.
    """
    # Redact caller-supplied content before it is measured or sent.
    history = [_redacted_turn(t) for t in (history or [])]
    system = template.system
    # JSON-aware redaction for any variable the template declares as a JSON
    # document. This runs in the GENERIC path so every caller of complete()
    # gets it — not just the summarize_application() wrapper. Whole-string
    # PiiRedactor.redact (below) does not mask label-only identifiers
    # (name/DOB/address/EIN/employer); redact_json does, via _is_identity_key.
    # Fails closed on invalid JSON (see _redact_json_var) so a malformed payload
    # cannot fall back to weaker whole-string redaction and leak those fields. A
    # non-string value (a caller passing the application as a dict) is serialized
    # to JSON first — otherwise it would render as str(dict) and skip masking.
    for var in getattr(template, "json_vars", ()):
        if var in variables:
            value = variables[var]
            if not isinstance(value, str):
                try:
                    value = json.dumps(value)
                except (TypeError, ValueError) as e:
                    raise LLMError(
                        f"{var} must be a JSON object or its JSON string; got a "
                        f"non-JSON-serializable {type(value).__name__}."
                    ) from e
            variables[var] = _redact_json_var(var, value)
    user_msg = {"role": "user",
                "content": PiiRedactor.redact(template.render_user(**variables))}
    example_msgs = _expand_examples(template.examples)

    # Reserve room for the answer so prompt + response stays under budget.
    reserved = max_tokens
    fixed_tokens = (
        estimate_tokens(system)
        + sum(estimate_tokens(m["content"]) for m in example_msgs)
        + estimate_tokens(user_msg["content"])
    )

    if fixed_tokens + reserved > token_budget:
        raise TokenBudgetExceeded(
            f"Request needs ~{fixed_tokens} input + {reserved} reserved answer "
            f"tokens, over the {token_budget} budget, before adding history. "
            f"Shorten the input or raise CLAUDE_TOKEN_BUDGET."
        )

    # Trim oldest history turns until everything fits.
    trimmed = 0
    while history:
        hist_tokens = sum(estimate_tokens(m["content"]) for m in history)
        if fixed_tokens + hist_tokens + reserved <= token_budget:
            break
        history.pop(0)  # drop oldest
        trimmed += 1

    messages = example_msgs + history + [user_msg]
    input_tokens = fixed_tokens + sum(estimate_tokens(m["content"]) for m in history)

    req = CompletionRequest(
        system=system,
        messages=messages,
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        idempotency_key=idempotency_key,
        metadata={"prompt": template.name, "prompt_version": template.version},
    )
    return BuiltRequest(
        request=req,
        estimated_input_tokens=input_tokens,
        trimmed_history_turns=trimmed,
    )
