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
from dataclasses import dataclass

from ..prompts import PromptTemplate
from ..redactor import PiiRedactor
from .adapter import CompletionRequest
from .errors import LLMError, TokenBudgetExceeded

_CHARS_PER_TOKEN = 4

# Value substituted for a direct-identity field before the application reaches
# the provider. Uses the redactor's • glyph so it reads as a redaction, not data.
_IDENTITY_MASK = "•••• (redacted)"


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

    The scalar is redacted *with its field label* (`"key": value`) so the
    label-gated patterns (bare SSN/phone, CVV) still fire, then the possibly
    masked value is returned as a plain string — so a masked number becomes the
    JSON string "…1111 (PAN)" rather than a bare, unparseable token.
    """
    if isinstance(value, bool) or value is None:
        return value
    prefix = f'"{key}": '
    probe = prefix + json.dumps(value)
    redacted = PiiRedactor.redact(probe)
    if redacted == probe:
        return value  # nothing sensitive — keep original type (numbers stay numbers)
    if not redacted.startswith(prefix):
        # Redaction altered the key/prefix (a PII-shaped field name); the
        # value slice would be misaligned. Fall back to redacting the value
        # alone — loses the label hint but stays correct and JSON-valid.
        return PiiRedactor.redact(value if isinstance(value, str) else json.dumps(value))
    masked = redacted[len(prefix):]
    if len(masked) >= 2 and masked[0] == '"' and masked[-1] == '"':
        masked = masked[1:-1]  # drop the quotes json.dumps added; re-quoted on output
    return masked


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


def _redacted_turn(turn: dict) -> dict:
    """Validate a caller-supplied message and redact PII from its content.

    Raises `LLMError` on a malformed turn (missing/ non-string content) instead
    of a bare KeyError, so callers get a typed failure.
    """
    if not isinstance(turn, dict) or "content" not in turn:
        raise LLMError("history turn must be a dict with a 'content' key")
    content = turn["content"]
    if not isinstance(content, str):
        raise LLMError("history turn 'content' must be a string")
    return {"role": turn.get("role", "user"), "content": PiiRedactor.redact(content)}


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
    request is built (ADR 0005 decision #2 — least privilege: raw PAN/CVV/SSN/
    email/phone must not leave the system to a third-party provider). System and
    few-shot examples are authored by us and carry no customer PII.

    Raises `TokenBudgetExceeded` if the non-trimmable parts plus the reserved
    answer room do not fit in `token_budget`. Raises `LLMError` on a malformed
    history turn.
    """
    # Redact caller-supplied content before it is measured or sent.
    history = [_redacted_turn(t) for t in (history or [])]
    system = template.system
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
