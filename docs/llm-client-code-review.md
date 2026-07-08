# Code Review — LLM API Client (origination-service)

**Reviewer:** Claude Code
**Date:** 2026-07-07
**Scope:** `services/origination-service/app/llm/*`, `app/prompts/*`, tests
**Design of record:** ADR 0005 (revised 2026-07-07)
**Checklist:** "Reviewing LLM-integration code — what to look for" (7 items)

Verdict: **Ship for Week 1.** Seven build concerns are implemented and tested
against a fake model (56 tests pass, no tokens spent). Findings below are ranked;
none block the Week-1 turn-in. Items marked *Deferred* are explicitly scoped to
Week 2 in ADR 0005.

---

## 1. Secrets — key from env only? Never logged/committed/echoed?

**Pass.**
- Key read only via `os.getenv("CLAUDE_API_KEY")` in `config.py`; missing key
  raises `LLMConfigError` at boot (`load_llm_config`), not on first call.
- `LLMConfig.redacted()` is the only config-to-log path and omits `api_key`;
  test `test_config_loads_defaults` asserts the key is absent.
- No key in error messages (`errors.py` messages describe modes, never content).
- Adapter holds the key in memory only; never logged.

**Note (existing debt, out of scope):** other secrets in this repo *are*
hardcoded (`app/config.py` EXPERIAN_KEY, CORE_BANKING_API_KEY) — tracked as D1 in
`docs/debt-log.md`. The LLM client does not repeat that mistake.

## 2. Isolation — all model calls behind the adapter?

**Pass.** Every provider call goes through `ModelAdapter.complete/stream`. The
`anthropic` SDK is imported only inside `ClaudeAdapter` (lazily), so no raw
provider call leaks into app code and the rest of the package imports without the
SDK. Business logic (budget, retry, validation, logging) lives in the client's
collaborators, not the adapter — the adapter is translation-only.

## 3. Budget — real token budgeting? Prompts from a library?

**Pass, with one caveat.**
- Real pre-flight budgeting in `request_builder`: counts system + examples +
  history + user, **reserves `max_tokens` for the answer**, trims oldest history
  to fit, and raises `TokenBudgetExceeded` before any network call
  (`test_token_budget_refused_preflight`, `test_history_trimmed_to_fit`).
- Prompts come from `app/prompts` (`get_prompt`), never inline strings.

**Finding F1 (low, correctness):** token counting is a `len/4` heuristic. It can
**undercount** JSON with many short tokens or non-English text, so a request the
guard admits could exceed the real budget at the provider. Acceptable for MVP
(budget has 10× headroom) but replace with the SDK's `count_tokens` or a
tokenizer before tightening the budget. Tracked in ADR 0005 "Future Work."

## 4. Resilience — timeouts? Bounded, backed-off, retryable-only? Idempotency?

**Pass, with one deliberate scoping call.**
- Timeout on every call (`CompletionRequest.timeout`, enforced by adapter;
  surfaces as `LLMTimeoutError`).
- Retry bounded (`1 + max_retries`), exponential backoff **with equal jitter**
  (`transport._backoff_delay`, `test_backoff_grows_with_jitter`).
- Retries **only** 429/5xx (`LLMHTTPError.retryable`); 4xx raise immediately
  (`test_4xx_not_retried`).
- Idempotency key threaded through and logged as `request_id`; safe because
  completion has no server side effect.

**Finding F2 (low, design):** timeouts are **not** retried — the checklist scopes
retry to 429/5xx, and timeout is a separate error. Defensible, but operationally
timeouts are often transient; revisit with real data. Documented in
`transport.py`.

## 5. Validation — parsed + schema-checked before use? Failure path defined?

**Pass (core); one part deferred.**
- Structured path: `guard_output` → `parse_json` → `validate_schema`
  (`validator.py`). Malformed JSON, wrong type, and bad enum all raise
  `ValidationFailed` (`test_malformed_output_raises`, `test_bad_enum_rejected`).
- Failure path defined: caller-supplied `fallback` returns a safe default,
  otherwise it raises — **never returns malformed output**
  (`test_fallback_on_bad_output`).

**Finding F3 (medium, completeness — Deferred):** **retry-with-correction** is
not implemented. On validation failure we fall back or raise; we do not re-prompt
the model with the parse error. Scoped to Week 2 in ADR 0005 (needs the
prompt-feedback loop). Until then, a flaky-formatting model degrades to the
fallback rather than self-healing.

**Finding F4 (low, robustness):** `validate_schema` is a hand-rolled JSON-Schema
subset (object/array/string/number/enum/required/additionalProperties). It covers
the loan prompt but silently ignores unknown keywords (e.g. `minLength`,
`pattern`). Fine now; swap in `jsonschema` if prompts grow.

## 6. Logging hygiene — could any secret or PII reach a log line?

**Pass.**
- Client logs **metrics only** (latency, token counts, model, retries,
  request id) — never the API key or raw request/response content.
- Belt-and-suspenders: the `llm` logger reuses the service's `RedactingFormatter`
  (`logging_setup.py`), so any line is redacted regardless.
- **Leak guard**: `guard_output` refuses model output that still contains
  detectable PII before it is returned *or* logged
  (`test_leak_guard_blocks_pii_in_output`).
- End-to-end check: `test_no_pii_in_logs` feeds SSN/PAN/email/phone in the input
  and asserts none appears in the captured log stream.

**Finding F5 (low, residual risk):** the leak guard reuses `PiiRedactor`, so it
inherits the redactor's blind spots — a PII format the regex misses (e.g.
international phone, IBAN) would pass both redactor and guard. This is shared
debt with D2/logging, not new. Mitigation: the primary control is that we never
log content and instruct the model not to emit PII; the guard is a backstop.

## 7. Unhappy path tested with a fake model?

**Pass.** All 18 client tests use `FakeAdapter` — no network, no tokens, no SDK.
Covered: config failure, 429/5xx retry, 4xx no-retry, retries-exhausted, timeout,
backoff+jitter, budget refusal, history trimming, malformed JSON, bad enum,
fallback, leak guard, adapter wiring, and no-PII-in-logs.

**Finding F6 (low, coverage):** no test exercises the real `ClaudeAdapter`
translation (`_translate_error`, response parsing) — it needs the SDK + a live
key, deliberately excluded from unit tests. Add a gated smoke test (skipped
without `CLAUDE_API_KEY`) per ADR 0005 acceptance #15 before Week-2 launch.

---

## Findings summary

| ID | Sev | Area | Status |
|----|-----|------|--------|
| F1 | low | token estimate is `len/4`, can undercount | accept for MVP; real tokenizer later |
| F2 | low | timeouts not retried | deliberate; revisit with data |
| F3 | med | retry-with-correction missing | **Deferred to Week 2 (ADR 0005)** |
| F4 | low | hand-rolled schema subset | accept; `jsonschema` if prompts grow |
| F5 | low | leak guard inherits redactor blind spots | shared debt; backstop only |
| F6 | low | no live-adapter smoke test | add gated smoke test before Week-2 launch |

No high-severity findings. No secret/PII leak path found. Recommend merge.
