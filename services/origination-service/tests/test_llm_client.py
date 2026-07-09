"""Unit tests for the LLM client (ADR 0005).

Everything runs against `FakeAdapter` — no network, no tokens, no `anthropic`
SDK required. Covers the unhappy paths the review checklist calls out: config
failure, timeout, 429/5xx retry, 4xx no-retry, token budget, malformed output,
leak guard — plus a check that no PII reaches the logs.
"""
import io
import json
import logging

import pytest

from app.llm import (
    BedrockAdapter,
    ClaudeAdapter,
    ClaudeClient,
    FakeAdapter,
    LLMConfig,
    LLMConfigError,
    TokenBudgetExceeded,
    ValidationFailed,
    load_llm_config,
)
from app.llm.adapter import Completion, CompletionRequest
from app.llm.errors import LLMError, LLMHTTPError, LLMTimeoutError
from app.llm.request_builder import build_request, estimate_tokens, redact_json
from app.llm.transport import call_with_retry
from app.llm.validator import parse_json
from app.prompts import get_prompt
from app.redactor import PiiRedactor

# A valid loan-summary JSON the fake model can "return".
GOOD_SUMMARY = (
    '{"summary": "Applicant requests $10,000 over 36 months.", '
    '"risk_flags": ["verify income"], '
    '"recommended_next_step": "request_docs"}'
)


def _config(**over):
    base = dict(api_key="test-key", max_retries=2, token_budget=20_000, max_tokens=256)
    base.update(over)
    return LLMConfig(**base)


def _req(**over):
    base = dict(
        system="s", messages=[{"role": "user", "content": "hi"}],
        model="m", max_tokens=10, temperature=0.0, timeout=1.0,
    )
    base.update(over)
    return CompletionRequest(**base)


# --- Concern 1: config, fail loud at boot ---------------------------------

def test_config_missing_key_raises(monkeypatch):
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    with pytest.raises(LLMConfigError):
        load_llm_config()


def test_config_loads_defaults(monkeypatch):
    monkeypatch.setenv("CLAUDE_API_KEY", "k")
    cfg = load_llm_config()
    assert cfg.api_key == "k"
    assert cfg.model.startswith("claude-")
    assert "api_key" not in cfg.redacted()  # never expose the key


def test_key_never_in_repr_or_str():
    """The credential must not leak via repr/str (log.info(cfg), tracebacks)."""
    secret = "sk-super-secret-value-123"
    cfg = LLMConfig(api_key=secret)
    assert secret not in repr(cfg)
    assert secret not in str(cfg)
    assert secret not in str(cfg.redacted())
    assert secret not in "%s" % cfg  # format path used by loggers


def test_key_not_logged_on_call_or_error(caplog):
    """No code path logs the credential — success or failure."""
    secret = "sk-leak-canary-9999"
    cfg = _config(api_key=secret)
    buf = io.StringIO()
    from app.logging_config import RedactingFormatter
    handler = logging.StreamHandler(buf)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    llm_log = logging.getLogger("llm")
    llm_log.addHandler(handler)
    try:
        # success path
        ClaudeClient(cfg, adapter=FakeAdapter(response=GOOD_SUMMARY)) \
            .summarize_application('{"amount": 1}')
        # error path (transport failure gets logged)
        with pytest.raises(LLMHTTPError):
            ClaudeClient(cfg, adapter=FakeAdapter(
                raises=[LLMHTTPError("bad", 400, retryable=False)])) \
                .summarize_application('{"amount": 1}')
    finally:
        llm_log.removeHandler(handler)
    assert secret not in buf.getvalue()


# --- Provider selection (anthropic vs. bedrock) ---------------------------

def test_default_provider_is_anthropic(monkeypatch):
    monkeypatch.setenv("CLAUDE_API_KEY", "k")
    monkeypatch.delenv("CLAUDE_PROVIDER", raising=False)
    cfg = load_llm_config()
    assert cfg.provider == "anthropic"
    client = ClaudeClient(cfg)
    assert isinstance(client.adapter, ClaudeAdapter)


def test_bedrock_provider_does_not_require_api_key(monkeypatch):
    monkeypatch.delenv("CLAUDE_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_PROVIDER", "bedrock")
    cfg = load_llm_config()  # must not raise LLMConfigError without CLAUDE_API_KEY
    assert cfg.api_key == ""
    assert cfg.model.startswith("us.anthropic.")
    client = ClaudeClient(cfg)
    assert isinstance(client.adapter, BedrockAdapter)


def test_unknown_provider_rejected(monkeypatch):
    monkeypatch.setenv("CLAUDE_API_KEY", "k")
    monkeypatch.setenv("CLAUDE_PROVIDER", "openai")
    with pytest.raises(LLMConfigError):
        load_llm_config()


def test_injected_adapter_overrides_provider():
    """An explicitly injected adapter always wins, regardless of provider."""
    cfg = _config(provider="bedrock")
    client = ClaudeClient(cfg, adapter=FakeAdapter(response=GOOD_SUMMARY))
    assert isinstance(client.adapter, FakeAdapter)


def test_bedrock_adapter_without_sdk_raises_llm_error(monkeypatch):
    """Same lazy-import contract as ClaudeAdapter: no `anthropic[bedrock]`
    installed (no `boto3`) surfaces as a typed LLMHTTPError, not ImportError."""
    import builtins

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "anthropic":
            raise ImportError("simulated: anthropic[bedrock] not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    adapter = BedrockAdapter()
    with pytest.raises(LLMHTTPError):
        adapter.complete(_req())


def test_bedrock_missing_boto3_extra_raises_typed_error(monkeypatch):
    """Realistic 'shipped set missing the bedrock extra' (PR review F3): the base
    `anthropic` package imports fine, but `AnthropicBedrock()` construction fails
    because the `[bedrock]` extra (boto3) is absent. Must surface as a typed
    LLMHTTPError, not a raw ImportError leaking from _sdk_client()."""
    import sys
    import types

    fake = types.ModuleType("anthropic")

    def _needs_boto3(*args, **kwargs):
        raise ImportError("No module named 'boto3'")

    fake.AnthropicBedrock = _needs_boto3
    monkeypatch.setitem(sys.modules, "anthropic", fake)

    adapter = BedrockAdapter()
    with pytest.raises(LLMHTTPError):
        adapter.complete(_req())


# --- Concern 4: transport (timeout / retry) -------------------------------

def test_retries_5xx_then_succeeds():
    adapter = FakeAdapter(
        response=GOOD_SUMMARY,
        raises=[LLMHTTPError("boom", 503, retryable=True)],
    )
    out = call_with_retry(adapter, _req(), max_retries=2,
                          sleep=lambda _: None, rng=lambda: 0.0)
    assert out.text == GOOD_SUMMARY
    assert len(adapter.calls) == 2  # one failure + one success


def test_429_is_retried():
    adapter = FakeAdapter(response="ok",
                          raises=[LLMHTTPError("rate", 429, retryable=True)])
    call_with_retry(adapter, _req(), max_retries=1, sleep=lambda _: None,
                    rng=lambda: 0.0)
    assert len(adapter.calls) == 2


def test_4xx_not_retried():
    adapter = FakeAdapter(raises=[LLMHTTPError("bad", 400, retryable=False)])
    with pytest.raises(LLMHTTPError):
        call_with_retry(adapter, _req(), max_retries=3, sleep=lambda _: None,
                        rng=lambda: 0.0)
    assert len(adapter.calls) == 1  # no retry


def test_retries_exhausted_raises():
    adapter = FakeAdapter(raises=[
        LLMHTTPError("x", 500, retryable=True),
        LLMHTTPError("x", 500, retryable=True),
        LLMHTTPError("x", 500, retryable=True),
    ])
    with pytest.raises(LLMHTTPError):
        call_with_retry(adapter, _req(), max_retries=2, sleep=lambda _: None,
                        rng=lambda: 0.0)
    assert len(adapter.calls) == 3  # 1 + 2 retries


def test_timeout_not_retried():
    adapter = FakeAdapter(raises=[LLMTimeoutError("slow")])
    with pytest.raises(LLMTimeoutError):
        call_with_retry(adapter, _req(), max_retries=3, sleep=lambda _: None,
                        rng=lambda: 0.0)
    assert len(adapter.calls) == 1


def test_backoff_grows_with_jitter():
    delays = []
    adapter = FakeAdapter(response="ok", raises=[
        LLMHTTPError("x", 500, retryable=True),
        LLMHTTPError("x", 500, retryable=True),
    ])
    call_with_retry(adapter, _req(), max_retries=3,
                    sleep=lambda d: delays.append(d), rng=lambda: 1.0)
    # rng=1.0 => equal-jitter max: 2**attempt (1, 2)
    assert delays == [1.0, 2.0]


def test_client_recovers_from_retryable_failure():
    """Regression: ClaudeClient.complete() always passes `on_retry` to
    call_with_retry (unlike the tests above, which call it directly and never
    exercise that argument). A prior bug referenced the except-clause `exc`
    binding after Python had already unbound it, so any real 429/5xx crashed
    with UnboundLocalError instead of retrying — this drives the retry through
    the actual client call site to catch that class of regression.

    No `sleep`/`rng` injection available at this call site (the client doesn't
    expose one), so this incurs one real backoff sleep (<1s, attempt 0)."""
    adapter = FakeAdapter(
        response=GOOD_SUMMARY,
        raises=[LLMHTTPError("boom", 503, retryable=True)],
    )
    client = ClaudeClient(_config(), adapter=adapter)
    out = client.summarize_application('{"amount": 10000}')
    assert out["recommended_next_step"] == "request_docs"
    assert len(adapter.calls) == 2  # one failure + one success


# --- Concern 3: request builder / cost guard ------------------------------

def test_token_budget_refused_preflight():
    cfg = _config(token_budget=5)  # absurdly small
    client = ClaudeClient(cfg, adapter=FakeAdapter(response=GOOD_SUMMARY))
    with pytest.raises(TokenBudgetExceeded):
        client.summarize_application('{"amount": 10000}')


def test_history_trimmed_to_fit():
    tmpl = get_prompt("loan_application_summary")
    # History must be valid JSON (fail-closed); pad a JSON string to ~1000 tokens.
    long_turn = {"role": "user", "content": '{"note": "' + "x" * 3980 + '"}'}
    built = build_request(
        tmpl, model="m", max_tokens=256, temperature=0.0, timeout=1.0,
        token_budget=1200, history=[long_turn, long_turn, long_turn],
        application_json="{}",
    )
    assert built.trimmed_history_turns >= 1  # oldest dropped to fit


def test_estimate_tokens_ceil():
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


# --- Concern 6: validation / guardrails -----------------------------------

def test_summarize_returns_validated_dict():
    client = ClaudeClient(_config(), adapter=FakeAdapter(response=GOOD_SUMMARY))
    out = client.summarize_application('{"amount": 10000, "term_months": 36}')
    assert out["recommended_next_step"] == "request_docs"
    assert isinstance(out["risk_flags"], list)


def test_malformed_output_raises():
    client = ClaudeClient(_config(), adapter=FakeAdapter(response="not json"))
    with pytest.raises(ValidationFailed):
        client.summarize_application("{}")


def test_bad_enum_rejected():
    bad = ('{"summary": "s", "risk_flags": [], '
           '"recommended_next_step": "fund_immediately"}')
    client = ClaudeClient(_config(), adapter=FakeAdapter(response=bad))
    with pytest.raises(ValidationFailed):
        client.summarize_application("{}")


def test_fallback_on_bad_output():
    client = ClaudeClient(_config(), adapter=FakeAdapter(response="garbage"))
    out = client.summarize_application("{}", fallback={"summary": "unavailable"})
    assert out == {"summary": "unavailable"}


def test_leak_guard_blocks_pii_in_output():
    leaky = ('{"summary": "SSN 412-55-9981 on file", "risk_flags": [], '
             '"recommended_next_step": "request_docs"}')
    client = ClaudeClient(_config(), adapter=FakeAdapter(response=leaky))
    with pytest.raises(ValidationFailed):
        client.summarize_application("{}")


def _json_uescape(text, chars):
    r"""Rewrite each char in `chars` as a JSON \uXXXX escape (e.g. '@' -> @),
    building the backslash with chr(92) so this source file carries no literal
    escape sequence. Reproduces a model that JSON-escapes PII in its output: the
    escaped form is invisible to the raw leak guard's regexes, but json.loads
    decodes it straight back to real PII."""
    backslash = chr(92)
    return "".join(
        (backslash + "u%04x" % ord(c)) if c in chars else c for c in text
    )


@pytest.mark.parametrize("plain, escape", [
    ("contact maria@example.com", "@"),   # escaped '@' -> no email in raw text
    ("SSN 412-55-9981 on file", "-"),     # escaped '-' -> no dashed SSN in raw text
    ("card 4111-1111-1111-1111", "-"),    # escaped '-' -> no 13+ digit PAN run in raw
])
def test_leak_guard_blocks_escaped_pii_in_structured_output(plain, escape):
    """Regression (Codex): a model can JSON-escape PII so the raw leak guard's
    regexes never fire — an escaped '@' hides an email, an escaped '-' hides a
    dashed SSN / hyphen-grouped PAN — yet json.loads decodes each back to real
    PII. validate_structured re-guards the DECODED, re-serialized object, so the
    escaped output is rejected instead of handed back to the caller."""
    escaped = _json_uescape(plain, escape)
    leaky = ('{"summary": "%s", "risk_flags": [], '
             '"recommended_next_step": "request_docs"}') % escaped

    # Sanity 1: the RAW guard does NOT catch it — the leak is real, not masked
    # upstream. If this raised, the test would pass for the wrong reason.
    from app.llm.validator import guard_output
    guard_output(leaky)  # must not raise while the PII is still escaped
    # Sanity 2: decoding really does reconstitute the PII (guards against a typo
    # in the escape making the payload harmless).
    assert plain in json.loads(leaky)["summary"]

    # The full structured path must reject the decoded PII.
    client = ClaudeClient(_config(), adapter=FakeAdapter(response=leaky))
    with pytest.raises(ValidationFailed):
        client.summarize_application("{}")


# --- Concern 2: adapter is thin / injectable ------------------------------

def test_adapter_receives_built_request():
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    client = ClaudeClient(_config(), adapter=adapter)
    client.summarize_application('{"amount": 1}', idempotency_key="req-123")
    sent = adapter.calls[0]
    assert sent.idempotency_key == "req-123"
    assert sent.metadata["prompt"] == "loan_application_summary"


# --- Acceptance: no PII in logs -------------------------------------------

def test_no_pii_in_logs():
    """Feed PII in the input; assert none reaches the (redacted) log stream."""
    buf = io.StringIO()
    from app.logging_config import RedactingFormatter
    handler = logging.StreamHandler(buf)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    llm_log = logging.getLogger("llm")
    llm_log.addHandler(handler)
    try:
        client = ClaudeClient(_config(), adapter=FakeAdapter(response=GOOD_SUMMARY))
        client.summarize_application(
            '{"name": "Maria", "ssn": "412-55-9981", "email": "maria@example.com", '
            '"pan": "4111111111111111", "phone": "555-123-4567"}'
        )
    finally:
        llm_log.removeHandler(handler)
    logs = buf.getvalue()
    assert "412-55-9981" not in logs
    assert "maria@example.com" not in logs
    assert "4111111111111111" not in logs
    assert "555-123-4567" not in logs


# --- Adversarial fixes (A / C / D / E / B) --------------------------------

def test_pii_redacted_before_sent_to_provider():
    """Fix A: customer PII must be redacted before the request leaves to the
    third-party model (ADR 0005 decision #2), not just in output/logs."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).summarize_application(
        '{"name": "Maria", "ssn": "412-55-9981", "email": "maria@example.com", '
        '"pan": "4111111111111111", "phone": "555-123-4567"}'
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    assert "412-55-9981" not in sent
    assert "4111111111111111" not in sent
    assert "maria@example.com" not in sent
    assert "555-123-4567" not in sent


def test_history_pii_redacted_before_send():
    """Fix A: shaped PII in a structured JSON history field is masked (audit
    last-4 kept)."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).complete(
        "loan_application_summary",
        application_json="{}",
        history=[{"role": "user", "content": '{"ssn": "412-55-9981"}'}],
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    assert "412-55-9981" not in sent
    assert "9981" in sent  # shaped SSN masked, audit last-4 kept


def test_free_text_purpose_field_does_not_leak_identity():
    """Regression (review): a valid application object still leaked identity via
    applicant-controlled free text — purpose is a non-identity key, so its value
    went through the pattern pass only, which cannot detect a name/DOB/address.
    A whitespace-bearing (unstructured) purpose is now masked wholesale; the
    structured triage fields survive."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).summarize_application(
        '{"name": "Jane Smith", '
        '"purpose": "medical loan for Jane Smith DOB 1970-01-01 at 10 Main St", '
        '"amount": 1000, "term_months": 24}'
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    for leaked in ("Jane Smith", "DOB 1970-01-01", "10 Main St"):
        assert leaked not in sent, f"identity leaked via purpose: {leaked!r}"
    assert "1000" in sent        # structured triage fields survive
    assert "24" in sent


def test_structured_purpose_code_survives():
    """A structured purpose code (no whitespace) is operational data and is kept
    — only unstructured free text is masked."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).summarize_application(
        '{"purpose": "debt_consolidation", "amount": 5000}'
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    assert "debt_consolidation" in sent
    assert "5000" in sent


def test_no_whitespace_free_text_still_masked():
    """Regression (review): a no-whitespace string under a non-identity key used
    to fall straight through to the pattern pass, which has no name/plain-date
    shape — so a hyphenated/concatenated name or an ISO date leaked raw. The
    free-text guard is now FAIL-CLOSED on shape (only lowercase code tokens pass),
    so these are masked despite carrying no whitespace."""
    for leaked in ("Jane-Smith", "JaneSmith", "1970-01-01"):
        adapter = FakeAdapter(response=GOOD_SUMMARY)
        ClaudeClient(_config(), adapter=adapter).summarize_application(
            '{"purpose": "%s", "amount": 1000}' % leaked
        )
        sent = "".join(m["content"] for m in adapter.calls[0].messages)
        assert leaked not in sent, f"identity leaked via no-whitespace purpose: {leaked!r}"
        assert "1000" in sent  # numeric triage field still survives


def test_free_text_with_embedded_shaped_pii_masked_wholesale():
    """Free text is masked WHOLESALE, not just its shaped PII: a purpose that
    embeds an SSN also carries a label-less name around it. Pattern-masking only
    the SSN would leak the name, so the whole value is dropped."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).summarize_application(
        '{"purpose": "call Jane Smith 412-55-9981", "amount": 1000}'
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    for leaked in ("Jane Smith", "9981", "412-55-9981"):
        assert leaked not in sent, f"free text leaked: {leaked!r}"
    assert "1000" in sent


def test_history_free_text_field_value_redacted():
    """A free-text (whitespace-bearing) value under a non-identity history field
    is masked wholesale — it can hide label-less identity the pattern pass can't
    catch, so it is not sent raw."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).complete(
        "loan_application_summary",
        application_json="{}",
        history=[{"role": "user", "content":
                  '{"note": "prior borrower Jane Smith DOB 1970-01-01 at 10 Main St"}'}],
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    for leaked in ("Jane Smith", "1970-01-01", "10 Main St"):
        assert leaked not in sent


def test_history_identity_fields_not_sent_to_provider():
    """Regression (review): history is caller-supplied and can replay raw
    application JSON. Label-only identifiers (name/DOB/address/employer) have no
    shape for the pattern redactor, so a JSON history turn gets the same
    JSON-aware masking as the current message — none of the values reach the
    adapter, while shaped PII keeps its audit last-4 and triage fields survive."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).complete(
        "loan_application_summary",
        application_json="{}",
        history=[
            {"role": "user", "content": (
                '{"name": "Jane Smith", "dob": "1980-01-02", '
                '"address": "1 Main St", "employer": "Acme Corp", '
                '"ssn": "412-55-9981", "amount": 18000}'
            )},
            {"role": "user", "content": (
                '{"full_name": "Bob Roe", "date_of_birth": "1975-05-05"}'
            )},
        ],
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    for leaked in ("Jane Smith", "1980-01-02", "1 Main St", "Acme Corp",
                   "Bob Roe", "1975-05-05"):
        assert leaked not in sent, f"identity value leaked via history: {leaked!r}"
    assert "412-55-9981" not in sent      # shaped SSN masked...
    assert "9981" in sent                 # ...last 4 kept for audit
    assert "18000" in sent                # triage-relevant field survives


def test_free_prose_history_is_rejected(monkeypatch):
    """Regression (review): free-form history prose cannot be identity-masked
    (an unlabeled name is indistinguishable from ordinary text), so a non-JSON
    history turn FAILS CLOSED — LLMError before any provider call — rather than
    shipping name/DOB/address/employer raw."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    client = ClaudeClient(_config(), adapter=adapter)
    with pytest.raises(LLMError):
        client.complete(
            "loan_application_summary",
            application_json="{}",
            history=[{"role": "user", "content": (
                "Prior borrower Jane Smith, DOB 1970-01-01, address 10 Main St, "
                "employer Acme Corp."
            )}],
        )
    assert adapter.calls == []  # refused before the network


def test_malformed_application_json_is_rejected(monkeypatch):
    """Regression (review): a declared JSON var that is not valid JSON must fail
    closed. redact_json's whole-string fallback would leave label-only
    identifiers (name/DOB/address/employer) intact, so a malformed payload is
    refused before send instead of being partially redacted."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    client = ClaudeClient(_config(), adapter=adapter)
    with pytest.raises(LLMError):
        client.summarize_application(
            "{name: Jane Smith, dob: 1970-01-01, address: 10 Main St, amount: 10000}"
        )
    assert adapter.calls == []  # refused before the network


@pytest.mark.parametrize("payload", [
    '"Jane Smith DOB 1970-01-01 address 10 Main St"',  # bare JSON string
    '["Jane Smith", "1970-01-01"]',                     # JSON array
    '42',                                               # bare JSON number
])
def test_bare_json_scalar_or_array_application_json_rejected(payload):
    """Adversarial: prose wrapped in quotes/brackets is valid JSON but has no
    keys to mask, so it would ship identity raw. A declared JSON var must be an
    OBJECT; a bare scalar or array fails closed like malformed input."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    with pytest.raises(LLMError):
        ClaudeClient(_config(), adapter=adapter).summarize_application(payload)
    assert adapter.calls == []


def test_dict_application_json_is_masked_not_stringified(monkeypatch):
    """Adversarial: a caller passing the application as a dict (not a JSON
    string) must not skip masking. The value is serialized to JSON and redacted;
    it must never render as str(dict) with identity intact."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).complete(
        "loan_application_summary",
        application_json={"name": "Jane Smith", "dob": "1970-01-01", "amount": 100},
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    assert "Jane Smith" not in sent
    assert "1970-01-01" not in sent
    assert "100" in sent  # triage field survives


@pytest.mark.parametrize("content", [
    '"Prior borrower Jane Smith, DOB 1970-01-01, 10 Main St"',  # bare JSON string
    '["Jane Smith", "1970-01-01"]',                             # JSON array
])
def test_bare_json_scalar_or_array_history_rejected(content):
    """Adversarial: the same quotes/brackets-as-prose bypass on history. A turn
    must be a JSON object, not a bare scalar/array, or it fails closed."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    with pytest.raises(LLMError):
        ClaudeClient(_config(), adapter=adapter).complete(
            "loan_application_summary",
            application_json="{}",
            history=[{"role": "user", "content": content}],
        )
    assert adapter.calls == []


def test_stream_is_gated_not_leaking():
    """Fix C: stream() bypasses output guards, so it raises rather than
    shipping raw, unvalidated model text until buffer-then-validate lands."""
    client = ClaudeClient(_config(), adapter=FakeAdapter(response=GOOD_SUMMARY))
    with pytest.raises(NotImplementedError):
        list(client.stream("loan_application_summary", application_json="{}"))


def test_parse_json_single_line_fence():
    """Fix D: a single-line ```json {...}``` fence parses, not rejected."""
    assert parse_json('```json {"a": 1} ```') == {"a": 1}
    assert parse_json('```\n{"b": 2}\n```') == {"b": 2}


def test_malformed_history_raises_typed_error():
    """Fix E: a history turn without 'content' raises LLMError, not KeyError."""
    tmpl = get_prompt("loan_application_summary")
    with pytest.raises(LLMError):
        build_request(
            tmpl, model="m", max_tokens=10, temperature=0.0, timeout=1.0,
            token_budget=20_000, history=[{"role": "user"}], application_json="{}",
        )


def test_numeric_pii_json_stays_valid_and_redacted():
    """Fix F2: PII encoded as JSON *numbers* must be redacted without corrupting
    the JSON the prompt hands the model. Whole-string redaction would replace the
    bare numeric literals with unquoted mask text; the JSON-aware path keeps the
    document parseable while still masking the PII."""
    import json as _json

    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).summarize_application(
        '{"name": "the applicant", "ssn": 412559981, "card": 4111111111111111, '
        '"phone": 5551234567, "amount": 18000}'
    )
    messages = adapter.calls[0].messages
    sent = "".join(m["content"] for m in messages)
    # The user message carries our payload (few-shot examples come first).
    user_msg = messages[-1]["content"]
    block = user_msg.split("Application (JSON):\n", 1)[1].split("\n\n", 1)[0]
    parsed = _json.loads(block)  # raises if redaction broke the JSON
    # Non-PII numbers keep their type/value.
    assert parsed["amount"] == 18000
    # Raw PII must not survive anywhere in the sent request.
    assert "412559981" not in sent
    assert "4111111111111111" not in sent
    assert "5551234567" not in sent


def test_account_and_routing_numbers_not_sent_to_provider():
    """No-ship fix: bank account + routing numbers in a loan application must be
    masked BEFORE the prompt reaches the third-party model (ADR 0005: account
    identifiers must not leave the system). Asserts the raw values are absent from
    every message actually handed to the adapter."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).summarize_application(
        '{"name": "the applicant", "account_number": 5551234567, '
        '"routing_number": 123456789, "iban": "GB82WEST12345698765432", '
        '"amount": 18000}'
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    assert "5551234567" not in sent          # account number masked
    assert "123456789" not in sent           # routing number masked
    assert "GB82WEST12345698765432" not in sent  # IBAN masked
    assert "18000" in sent                    # non-PII amount preserved


def test_identity_fields_not_sent_to_provider():
    """No-ship fix: direct applicant identifiers (name, DOB, address, EIN,
    employer) have no self-identifying shape for the pattern redactor, so they
    would reach the third-party model raw. ADR 0005 least-privilege: they must be
    generalized before the prompt is built. Asserts the raw values are absent
    from every message actually handed to the adapter, while triage-relevant
    non-identity fields survive."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).summarize_application(
        '{"name": "Jane Doe", "dob": "1970-01-01", "address": "10 Main St", '
        '"ein": "12-3456789", "employer": "Acme Corp", '
        '"annual_income": 42000, "amount": 18000, "purpose": "auto"}'
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    assert "Jane Doe" not in sent          # name generalized
    assert "1970-01-01" not in sent        # DOB dropped
    assert "10 Main St" not in sent        # address dropped
    assert "12-3456789" not in sent        # EIN dropped
    assert "Acme Corp" not in sent         # employer dropped
    assert "42000" in sent                 # income kept (triage-relevant)
    assert "18000" in sent                 # amount kept
    assert "auto" in sent                  # purpose kept


def test_identity_masking_enforced_in_public_complete_path():
    """Regression (review): JSON-aware identity masking must run in the generic
    complete() path, not only in the summarize_application() wrapper. complete()
    is public and takes application_json directly, so a caller that bypasses the
    wrapper must NOT be able to ship raw name/DOB/EIN/employer to the provider.
    Masking is driven by the prompt's declared json_vars in build_request."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).complete(
        "loan_application_summary",
        application_json=(
            '{"name": "Jane Doe", "dob": "1970-01-01", "ein": "12-3456789", '
            '"employer": "Acme Corp", "annual_income": 42000, "amount": 18000}'
        ),
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    assert "Jane Doe" not in sent
    assert "1970-01-01" not in sent
    assert "12-3456789" not in sent
    assert "Acme Corp" not in sent
    assert "42000" in sent                 # triage-relevant fields survive
    assert "18000" in sent


def test_nested_identity_fields_not_sent_to_provider():
    """Identity fields nested under a non-identity parent are still gated by the
    per-key walk — a structured address object is dropped wholesale."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).summarize_application(
        '{"applicant": {"full_name": "John Roe", "date_of_birth": "1985-02-03"}, '
        '"home_address": {"street": "42 Elm Ave", "city": "Springfield"}, '
        '"amount": 9000}'
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    assert "John Roe" not in sent
    assert "1985-02-03" not in sent
    assert "42 Elm Ave" not in sent
    assert "Springfield" not in sent
    assert "9000" in sent


def test_identity_key_variant_spellings_not_sent_to_provider():
    """Adversarial hardening: identity keys must be caught in concatenated and
    prefixed spellings too (firstname, surname, fullname, federal_ein, street1),
    and job_title (employment identity, like employer). Only underscore/exact
    forms were covered before, so these variants reached the provider raw."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).summarize_application(
        '{"firstname": "Jane", "lastname": "Roe", "fullname": "Jane Q Roe", '
        '"surname": "Roe", "middlename": "Quinn", "dateofbirth": "1970-01-01", '
        '"federal_ein": "98-7654321", "street1": "10 Main St", '
        '"job_title": "Chief Widget Officer", "employer": "Acme", "amount": 18000}'
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    for leaked in ("Jane", "Roe", "Quinn", "1970-01-01", "98-7654321",
                   "10 Main St", "Chief Widget Officer", "Acme"):
        assert leaked not in sent, f"identity value leaked: {leaked!r}"
    assert "18000" in sent  # operational field preserved


def test_is_identity_key_does_not_over_match_operational_fields():
    """The hardened matcher must not mask triage-relevant operational fields."""
    from app.llm.request_builder import _is_identity_key
    for k in ("amount", "income", "term_months", "purpose",
              "employment_years", "is_entity", "loan_id", "apr", "status"):
        assert not _is_identity_key(k), f"over-masked operational field {k!r}"
    for k in ("firstname", "surname", "fullname", "federal_ein",
              "street1", "job_title", "name", "dob", "ein", "address"):
        assert _is_identity_key(k), f"missed identity field {k!r}"


def test_pii_in_json_key_not_sent_to_provider():
    """F3-key: customer PII carried in an object KEY (not a value) must not reach
    the model. redact_json rebuilt objects with the original key untouched."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).summarize_application(
        '{"contact@ex.com": "note", "ssn": 412559981}'
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    assert "contact@ex.com" not in sent
    assert "412559981" not in sent


def test_labeled_number_variants_redacted_before_send():
    """F3-labels: `ssn_number` / `phone_number` (and variants) are common
    structured keys; bare numeric values under them must be masked pre-send."""
    adapter = FakeAdapter(response=GOOD_SUMMARY)
    ClaudeClient(_config(), adapter=adapter).summarize_application(
        '{"ssn_number": 412559981, "phone_number": 5551234567, "loan_number": 87654321}'
    )
    sent = "".join(m["content"] for m in adapter.calls[0].messages)
    assert "412559981" not in sent   # ssn_number masked
    assert "5551234567" not in sent  # phone_number masked
    assert "87654321" in sent        # unrelated labeled number left intact


def test_redact_json_preserves_last4_and_falls_back_on_bad_json():
    out = redact_json('{"ssn": 412559981}')
    assert out == '{"ssn": "•••-••-9981"}'
    # Non-JSON input degrades to whole-string redaction, never raises.
    assert redact_json("SSN 412-55-9981") == PiiRedactor.redact("SSN 412-55-9981")
    # A PII-shaped KEY must not corrupt the JSON or leak the value.
    import json as _json
    out = redact_json('{"contact@ex.com": "4111111111111111"}')
    parsed = _json.loads(out)  # still valid JSON
    assert "4111111111111111" not in out  # value redacted


def test_redactor_catches_spaced_ssn():
    """Fix B: space-separated SSN (XXX XX XXXX) is redacted, closing the leak
    guard hole found in the adversarial round."""
    out = PiiRedactor.redact("applicant SSN 412 55 9981 on file")
    assert "412 55 9981" not in out
    assert "9981" in out  # last-4 preserved for audit
