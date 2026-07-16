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

# Value substituted for an applicant-controlled FREE-TEXT field (e.g. `purpose`).
# A string value under a non-identity key can embed a name/DOB/address the pattern
# redactor cannot detect (no label, no shape), so — unless it is itself a genuine
# shaped token whose audit last-4 we keep — it must not reach the provider raw.
# FAIL-CLOSED: after the label-gated pattern pass, any leftover non-empty string
# under a non-identity key is masked. No shape rule can tell an operational code
# (auto, debt_consolidation) from a lowercased bare name (jane, jane_smith) — they
# are byte-identical — so we do not try; strings are masked, numbers survive.
_FREETEXT_MASK = "•••• (free text redacted)"

# Value substituted for an object KEY that is not a field name. A legitimate key is
# a schema label (a bare identifier token); a key carrying data (`{"Jane Smith": 1}`,
# `{"dob 1970-01-01": 2}`) is caller-supplied identity the pattern redactor cannot
# detect, and it would reach the provider raw. FAIL-CLOSED: non-field-name keys are
# masked wholesale (see _is_field_name / _redact_key).
_KEY_MASK = "•••• (key redacted)"

# Safe categorical fields: field NAME -> the controlled vocabulary its value may
# take. A value that matches (case-insensitive) is a known operational code and
# is passed to the provider raw, because it is a core underwriting fact the
# loan-summary prompt is built around (see prompts/loan_summary.py). Any value
# NOT in the set fails closed through the normal string masking below — this is
# a VALUE allowlist, not a field-name one: `purpose` is an unvalidated free-TEXT
# column, so a name/DOB stuffed into it ({"purpose": "jane_smith"}) is not a
# known code and is masked. Extend a set to admit a new legitimate code; an
# unlisted code degrades gracefully (masked, not leaked). Keep every entry a
# compound operational token that cannot collide with a plausible bare name.
_SAFE_CATEGORICAL = {
    "purpose": {
        "auto",
        "debt_consolidation",
        "home_improvement",
        "personal",
        "working_capital",
    },
    # Decisioning-assistant protocol vocabulary (ADR 0009 §5): the agent's history
    # turns are JSON objects whose string values are drawn from these closed enums
    # (plus numbers, which pass structurally). System-defined codes, not caller
    # data — none can collide with a plausible bare name.
    "action": {"tool", "final"},
    "tool": {"score_application", "get_decision_record"},
    "task": {"decision", "explain"},
    "outcome": {"approve", "refer", "deny", "counteroffer"},
    "policy_band": {"approve", "refer", "deny"},
    "status": {"recorded", "no_record_legacy", "not_found"},
    "reason_codes": {"r01", "r02", "r03", "r04"},
}


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
    if (
        "address" in kn
        or kn.startswith(("street", "zip", "postal", "postcode"))
        or kn == "city"
    ):
        return True
    # employer identification: ein (any prefix: federal_ein, employer_ein), employer*,
    # company; and job title (employment identity, like employer).
    if (
        kn == "ein"
        or kn.endswith("ein")
        or kn.startswith("employer")
        or kn == "company"
        or "jobtitle" in kn
        or kn == "title"
    ):
        return True
    return False


def _looks_like_numeric_identity(value) -> bool:
    """True if an integer has the digit-shape of a label-less identifier that the
    pattern pass cannot catch when it is packed as a JSON *number*.

    A bare SSN/DOB/phone written as a number carries no separators and no label,
    so the shaped-PII pass (which keys on separators, e.g. 3-2-4 for SSN, or on a
    field label) misses it and it would reach the provider raw. The pattern pass
    DOES still catch a card PAN as a number (Luhn/13-19-digit shape), so this only
    needs to cover the three shapes it cannot: a 9-digit SSN, an 8-digit YYYYMMDD
    date of birth, and a 10-digit NANP phone. Booleans are excluded (bool is an
    int subclass); floats are money-shaped (apr, rate) and never these IDs.

    Kept structural to avoid over-masking triage figures: amount/income/term are
    far shorter than 8 digits, and the 8-digit branch additionally requires a
    valid calendar date so a large loan number like 87654321 (year 8765) survives.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    digits = str(abs(value))
    n = len(digits)
    if n == 9:  # SSN — 9 digits, no separators
        return True
    if n == 8:  # possible YYYYMMDD date of birth
        year, month, day = int(digits[:4]), int(digits[4:6]), int(digits[6:8])
        return 1900 <= year <= 2099 and 1 <= month <= 12 and 1 <= day <= 31
    if n == 10:  # NANP phone: area-code and exchange lead digits are 2-9
        return digits[0] in "23456789" and digits[3] in "23456789"
    return False


def _is_numeric_identity_key(key) -> bool:
    """True if a field NAME denotes an SSN/TIN — an identifier of up to 9 digits
    that may arrive as a JSON *number* which lost its leading zero (012-34-5678
    coerced to the int 12345678). Such a value is neither 9 digits nor a valid
    date, so `_looks_like_numeric_identity` misses it and the label-gated pattern
    pass (which wants a 9-digit / 3-2-4 shape) misses it too. Under one of these
    labels any number is masked outright. Distinctive tokens (socialsecurity,
    taxpayer, taxid, itin, nationalid) match as substrings so spelling variants
    (social_security_no, tax_id_number) are caught; short/ambiguous tokens
    (ssn/tin/sin) are anchored so the "tin" inside "routing_number" or the "ssn"
    inside "cross_number" does not false-positive.
    """
    kn = str(key).strip().lower().replace("_", "").replace("-", "").replace(" ", "")
    # Distinctive identity tokens — safe as substrings (catches social_security_no,
    # taxpayer_id, tax_id_number, national_id, itin without an ordinary field name
    # embedding them by accident).
    if any(
        tok in kn
        for tok in ("socialsecurity", "taxpayer", "taxid", "itin", "nationalid")
    ):
        return True
    # Short/ambiguous tokens — anchored so a substring like the "tin" inside
    # "routing_number" or the "ssn" inside "cross_number" cannot false-positive.
    if kn == "ssn" or kn.startswith("ssn") or kn.endswith("ssn"):
        return True
    if kn == "tin" or kn.startswith("tin"):
        return True
    return kn in {"sin", "sinnumber"}


def _strip_fragment_digits(masked: str) -> str:
    """Drop residual identifier digits (the audit last-4 the PiiRedactor keeps)
    from a masked value before it is exported to the third-party model.

    PiiRedactor is a log/audit redactor: it preserves the last four digits of
    SSNs, phones, PANs and bank ids (•••-••-6789, ••••••••••••1111 (PAN)) so an
    operator can reconcile a record. Those fragments are stable partial
    identifiers and are nonessential to a triage summary, so least-privilege
    (ADR 0005) removes them for provider export while keeping the category shape
    (the dash layout, the "(PAN)" tag) as a harmless hint to the model.
    """
    return "".join("•" if ch.isdigit() else ch for ch in masked)


def _redact_scalar(key: str, value):
    """Redact one JSON scalar while keeping the result JSON-valid.

    Two-stage guard on a string value under a non-identity key:

    1. WHITESPACE ⇒ multi-token free text ⇒ masked WHOLESALE. Such a value can
       carry label-less identity around any shaped PII (`"call Jane Smith
       412-55-9981"`); pattern-masking only the SSN would leak "Jane Smith", so
       the whole value is dropped.
    2. NO WHITESPACE ⇒ a single token. It is redacted *with its field label*
       (`"key": value`) so the label-gated / shape patterns fire on a genuine
       single shaped token (an SSN/PAN/email is a token, not free text). Unlike
       the log redactor, the provider export drops the audit last-4 as well
       (`_strip_fragment_digits`) — a partial identifier is nonessential to a
       triage summary and must not cross the trust boundary. If nothing shaped
       matched, the string is masked outright:
       no shape rule can distinguish an operational code (auto) from a lowercased
       bare name (jane, jane_smith) — they are byte-identical — so a string under
       a non-identity key is never passed raw on shape alone. Numbers skip step 1
       and go through the pattern pass, which catches a card PAN literal (Luhn) —
       but NOT a bare 9-digit SSN, an 8-digit YYYYMMDD date of birth, or a 10-digit
       phone, which carry no separators/label as numbers. Those are matched
       structurally by `_looks_like_numeric_identity` and masked; every other
       number (triage figures: amount, income, term) survives. Empty string
       carries nothing, kept.
    """
    if isinstance(value, bool) or value is None:
        return value
    # Safe categorical value: a string under an allowlisted field whose value is
    # a KNOWN operational code (not a name/DOB hiding in an unvalidated field) is
    # a core underwriting fact and is passed raw. Match is case-insensitive; an
    # unlisted value falls through to the fail-closed masking below.
    if isinstance(value, str):
        allowed = _SAFE_CATEGORICAL.get(str(key).strip().lower())
        if allowed is not None and value.strip().lower() in allowed:
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
            return _strip_fragment_digits(
                PiiRedactor.redact(
                    value if isinstance(value, str) else json.dumps(value)
                )
            )
        masked = redacted[len(prefix) :]
        if len(masked) >= 2 and masked[0] == '"' and masked[-1] == '"':
            masked = masked[
                1:-1
            ]  # drop the quotes json.dumps added; re-quoted on output
        return _strip_fragment_digits(masked)
    # Nothing shaped. Fail closed: any non-empty string under a non-identity key
    # can hide label-less identity (a lowercased bare name is indistinguishable
    # from a code), so mask it outright.
    if isinstance(value, str) and value:
        return _FREETEXT_MASK
    # An SSN/TIN under its own label may arrive as a number that lost its leading
    # zero (012-34-5678 coerced to int 12345678) — neither 9 digits nor a valid
    # date, so the checks below miss it. Mask any number under an SSN/TIN label.
    if (
        _is_numeric_identity_key(key)
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return _IDENTITY_MASK
    # A bare SSN/DOB/phone packed as a JSON number has no separators/label for the
    # pattern pass to key on, so match it structurally and mask it. (A card PAN as
    # a number is already caught above.)
    if _looks_like_numeric_identity(value):
        return _IDENTITY_MASK
    return value  # triage numbers stay numbers; empty string carries nothing


def _is_field_name(s: str) -> bool:
    """True if a string is a plausible schema field NAME — a bare identifier
    token (`name`, `first_name`, `annualIncome`, `term-months`). A key that is not
    (contains whitespace, opens with a digit or symbol, or holds `.`/`@`/`:`) is
    caller data, not a label. Application/history keys in this system are all such
    tokens; names/DOB-text/addresses carry spaces or lead with a digit/symbol and
    fail this test.

    Residual (inherent, ACCEPTED — see tests/test_pii_matrix.py::test_documented
    _residual_bare_name_key). A no-separator bare name used as a KEY
    (`{"JaneSmith": 1}`) is byte-identical to a field name and passes. This is an
    asymmetry with the VALUE side, which fails closed: a bare name as a value
    (`{"purpose": "JaneSmith"}`) IS masked wholesale (_redact_scalar step 2). We
    cannot fail closed on key SHAPE the same way — every legitimate schema key
    (`amount`, `purpose`, `term_months`) is exactly a no-separator bare token, so
    masking that shape would destroy the payload. Accepted because keys in this
    system are system-defined schema/ADR-0009 vocab tokens, not caller free-text;
    a name-in-key needs a caller stuffing data into a key position. Closing it
    would require an allowlist of the canonical key set (deferred, not built).
    """
    if not s or not (s[0].isalpha() or s[0] == "_"):
        return False
    return all(ch.isalnum() or ch in "_-" for ch in s)


def _redact_key(key) -> str:
    """Redact PII out of an object key. Keys are normally field names, but a
    caller-supplied document could carry customer data in a key
    (`{"contact@ex.com": ...}`, `{"Jane Smith": 1}`); that key would otherwise
    reach the model raw. Shaped PII (email/SSN/PAN) is masked by the pattern
    redactor; anything that survives but is not a field-name token is masked
    wholesale, because a label-only identifier (name/DOB/address) in a key has no
    shape the redactor can catch — same fail-closed stance as string values.
    """
    redacted = PiiRedactor.redact(str(key))
    if not _is_field_name(redacted):
        return _KEY_MASK
    return redacted


def _redact_node(node, key: str = ""):
    if isinstance(node, dict):
        # Redact both the key (PII-in-key leak) and the value; the value is
        # redacted with the ORIGINAL key as its label so label-gated patterns
        # still fire before the key itself is masked. A direct-identity field
        # (name/DOB/address/EIN/employer) is generalized to a mask wholesale —
        # the pattern redactor cannot catch these (no self-identifying shape),
        # so the label is the gate and the entire subtree is dropped.
        #
        # Build explicitly (not a dict comprehension): distinct source keys can
        # redact to the SAME text — most often the _KEY_MASK constant when two
        # keys carry PII labels ({"Jane Smith": 1, "John Smith": 2}) — and a
        # comprehension would let the later key silently overwrite the earlier
        # value, dropping applicant facts before the model ever sees them. Keep
        # every value by disambiguating a collided key with a numeric suffix.
        out = {}
        for k, v in node.items():
            redacted_key = _redact_key(k)
            value = _IDENTITY_MASK if _is_identity_key(k) else _redact_node(v, k)
            if redacted_key in out:
                n = 2
                while f"{redacted_key} ({n})" in out:
                    n += 1
                redacted_key = f"{redacted_key} ({n})"
            out[redacted_key] = value
        return out
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

    Identity stuffed into a free-text value under a NON-identity key (e.g.
    {"notes": "Applicant Jane Smith"} or {"purpose": "jane_smith"}) is handled by
    `_redact_scalar`, which fails closed: every non-empty string scalar under a
    non-identity key is masked (whitespace-bearing free text wholesale, single
    tokens after a label-gated shaped-PII pass). Only numbers and shaped tokens
    (with their audit last-4) survive, so a label-less name cannot ride out in a
    string value.
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

    Raises `LLMError` on a malformed turn (missing/non-string content, content
    that is not a JSON object, or an unrecognized role) instead of a bare KeyError,
    so callers get a typed failure.
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
    # Validate the role too: it is copied straight onto the provider message and
    # is NOT redacted, so an unrecognized role (e.g. {"role": "Jane Smith"}) would
    # ship a raw, identity-bearing string across the trust boundary before the
    # provider rejects it. Normalize a missing role to "user"; fail closed on any
    # value other than the two roles the chat API accepts.
    role = turn.get("role", "user")
    if role not in ("user", "assistant"):
        raise LLMError(
            "history turn 'role' must be 'user' or 'assistant'; refusing to send "
            "an unrecognized role to the provider (it is not redacted and may "
            "carry identity)"
        )
    return {"role": role, "content": redact_json(content)}


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
    user_msg = {
        "role": "user",
        "content": PiiRedactor.redact(template.render_user(**variables)),
    }
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
