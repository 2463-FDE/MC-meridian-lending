# ADR 0005: LLM Client Design for Claude API Integration

- **Status:** Accepted
- **Date:** 2026-07-01
- **Author:** Claude Code

---

## Context

Meridian is building a loan-summary assistant to show the board "AI momentum" while hardening the LOS against PCI/security debt. The assistant will call Claude API to summarize loan applications for loan officers.

The LLM client must be:
- **Production-grade:** timeout, retry, structured output validation, cost guard.
- **Safe:** redacts PII before sending to Claude; never logs sensitive data.
- **Debuggable:** clear error messages, testable timeout/retry/validation paths.

The system has serious PCI debt (plaintext PAN/CVV/SSN in logs and schema, hardcoded credentials). The LLM client is the first step toward building safe infrastructure; the loan-summary feature is Week 2.

---

## Decision

We will build a `ClaudeClient` class in `services/origination-service/app/llm_client.py` with the following characteristics:

### Instantiation & Configuration

```python
ClaudeClient(
    api_key: str = os.getenv("CLAUDE_API_KEY"),
    model: str = "claude-haiku-4-5-20251001",
    timeout: float = 30.0,
    max_retries: int = 3,
    token_budget: int = 20_000,
)
```

- **API Key:** Read from env var `CLAUDE_API_KEY` at instantiation; raise `ValueError` if missing.
- **Model:** Hardcoded to `claude-haiku-4-5-20251001` (Haiku 4.5, fastest and cheapest for loan summarization). Override via env var `CLAUDE_MODEL` if needed.
- **Timeout:** 30 seconds (hardcoded; configurable at instantiation). Enforced via httpx timeout.
- **Max Retries:** 3 attempts for 5xx errors. 4xx errors fail immediately (no retry).
- **Token Budget:** 20,000 tokens per request (configurable). Request refused if prompt + expected response would exceed budget.

### Call Signature

```python
def call(
    messages: list[dict],  # OpenAI-compatible format
    response_schema: dict | None = None,  # JSON schema for structured output
) -> dict | str:
    """Call Claude API with timeout, retry, validation, cost guard."""
```

### Behavior

1. **Cost Guard (pre-flight):**
   - Estimate tokens in `messages` (rough count: ~4 chars per token).
   - Estimate response tokens (assume worst case: 50% of budget).
   - If `estimated_tokens > token_budget`, raise `TokenBudgetExceeded` with clear message.

2. **Request:**
   - POST to Claude API via `anthropic` SDK.
   - Redact PII from `messages` before sending (use `PiiRedactor` from logging redaction module).
   - Include `response_schema` as a JSON schema constraint if provided. **Planned approach:** use native Claude structured output *if the `anthropic` SDK version we pin supports it* — to be verified at implementation time. If it does not, fall back to a Pydantic model + manual validation (see Rationale).

3. **Timeout:**
   - Enforce 30s timeout on the HTTP call.
   - If timeout fires, raise `TimeoutError` with message "Claude API did not respond within 30s."

4. **Retry:**
   - On 5xx: exponential backoff (2^attempt seconds: 1s, 2s, 4s).
   - On 4xx: fail immediately with `HTTPError`.
   - On success: return response.

5. **Validation:**
   - If `response_schema` provided, validate response body against schema.
   - If malformed: raise `ValidationError` with details of what was expected vs. received.

6. **Logging:**
   - Log request (redacted) and response (redacted) at DEBUG level.
   - Never log API key, customer data, or full PII.
   - Use `PiiRedactor.redact()` on all logged text.

---

## Rationale

### API Key from Env Var
- **Why:** Simplest for MVP, works in Docker and local dev.
- **Alternative:** Secret manager (overkill for v1, deferred to week 2+).

### Haiku 4.5 Model
- **Why:** Fastest, cheapest (~$0.80 per 1M input tokens), appropriate for structured summaries. Loan application → summary is a simple task; Haiku handles it well.
- **Alternative:** Sonnet/Opus (overkill, 10–100x cost). Revisit in week 2 if summaries need higher quality.

### 30s Timeout
- **Why:** Loan officer workflow is synchronous; 30s is acceptable (typical Claude response: 2–5s). Prevents hanging on network issues.
- **Alternative:** Async background job (adds complexity; deferred).

### 3 Retries, Exponential Backoff
- **Why:** Handles transient 5xx from Claude API. Exponential backoff prevents hammering if API is degraded.
- **Alternative:** Circuit breaker (deferred to production hardening, week 3+).

### 20,000 Token Budget
- **Why:** Typical loan application + summary ≈ 2,000 tokens. 20k = 10x safety margin. Edge cases (long applications) won't fail unexpectedly. Cost impact: negligible (~$0.005 per call). Can tighten in week 2 based on observed usage.
- **Alternative:** 10k (tighter, but risks failures on edge cases during MVP).

### Redact PII Before Sending to Claude
- **Why:** Principle of least privilege. PII should never leave the system unless necessary. Claude API is a third party (Anthropic-hosted, US data centers). Even redacted data is safer than unredacted.
- **Alternative:** Send unredacted, redact only in logs (less secure; rejected).

### Native Claude Structured Output (planned, pending SDK verification)
- **Why:** If the pinned `anthropic` SDK exposes built-in structured-output constraints, using native support is safest and avoids reimplementing validation. **This has not yet been verified against a specific SDK version** — confirm at implementation time before relying on it.
- **Fallback:** If the SDK doesn't support it (or we can't confirm in time), use a Pydantic model + manual validation (origination-service already uses Pydantic 2.10.4).

### Single Dedicated Instance Per Service
- **Why:** Keep scope focused. origination-service is the primary consumer (week 1 loan-summary feature). If payment-service or others need LLM later, refactor to shared module then (YAGNI).
- **Alternative:** Shared library (adds infrastructure; deferred).

---

## Consequences

### Positive
- **Safe by default:** PII redacted before leaving the system.
- **Debuggable:** Clear errors (timeout, retry exhausted, budget exceeded, validation failed).
- **Testable:** Timeout, retry, validation paths are independently testable.
- **Cost-controlled:** Token budget prevents runaway costs.
- **Production-grade:** Timeout + retry handles real-world network issues.

### Negative
- **Env var dependency:** Operator must set `CLAUDE_API_KEY` in each environment. If missing, app fails at instantiation.
- **Single model hardcoded:** Changing models requires code change. Mitigated by env var override if needed.
- **Redaction pre-flight cost:** Estimating tokens on every call has CPU cost. Acceptable for MVP; optimize if profiling shows bottleneck.

### Future Work
- **Week 2+:** Move PiiRedactor to shared module (if other services need it).
- **Week 2+:** Add circuit breaker / fallback if Claude API is unavailable.
- **Week 3+:** Migrate to async if synchronous calls become bottleneck.
- **Week 3+:** Tighten token budget based on observed usage patterns.

---

## Compliance Notes

- **PCI-DSS:** LLM client never stores or logs PAN/CVV/SSN. Redaction ensures this. (D1 in debt-log.)
- **Privacy:** Redaction before sending to Claude satisfies privacy-first principle for customer data.
- **Auditability:** Redacted logs allow debugging without exposing sensitive data.

---

## Revision 2026-07-07: Expanded build checklist (7 concerns)

The original decision above described a single `ClaudeClient` class. Review added a
seven-concern build checklist. This revision folds those concerns into the design and
splits them into **Week 1 core** (build now) vs **Deferred** (later week), so the
build traces to an explicit line.

The client is decomposed into seven collaborators instead of one class, each mapping
to one checklist concern:

| # | Concern | Module | Scope | Notes |
|---|---------|--------|-------|-------|
| 1 | **Config** | `app/llm/config.py` | Week 1 | model id, default params, timeout in one `LLMConfig`; key from env; **fail loud at boot** (`load_llm_config()` raises `LLMConfigError` if `CLAUDE_API_KEY` missing — called at app startup, not lazily) |
| 2 | **Model adapter** | `app/llm/adapter.py` | Week 1 | one `ModelAdapter` interface (`complete()` + `stream()`) hiding the provider; translation only, no business logic; `ClaudeAdapter` (lazy `anthropic` import) + `FakeAdapter` for tests |
| 3 | **Request builder** | `app/llm/request_builder.py` | Week 1 | assembles system + examples + context + history + user; token budget (count, trim oldest history, reserve room for answer); pulls from prompt library, not inline |
| 4 | **Resilient transport** | `app/llm/transport.py` | Week 1 | timeout every call; retry only 429/5xx, never 4xx; exponential backoff **+ jitter**; idempotency key threaded through and logged as request id |
| 5 | **Streaming** | `adapter.stream()` | **Deferred** | interface defined + `ClaudeAdapter.stream()` implemented (buffer-then-validate), but not wired into a product path until the loan-summary feature (Week 2) needs a human-watching UI. Cancellation/mid-stream-failure hardening lands with that feature. |
| 6 | **Validator / guardrail** | `app/llm/validator.py` | Week 1 (core) / Deferred (correction loop) | parse + JSON-schema check; **fallback** to a safe default or raise on failure — never pass malformed forward; content/length/leak guards on output. **retry-with-correction** deferred to Week 2 (needs prompt-feedback loop). |
| 7 | **Logger** | reuses `logging_config.get_logger` + `PiiRedactor` | Week 1 | logs latency, token counts, model+params, retry count, request id; never the API key or raw PII (redactor already applied to all log output) |

### Orchestration

`ClaudeClient.summarize(...)` (and the lower-level `ClaudeClient.complete(...)`) wire
the collaborators in order: build request → cost guard → transport (timeout/retry) →
validate/guard → log metrics. The adapter is injected, so tests pass `FakeAdapter`
and spend no tokens.

### Idempotency (concern 4 detail)

LLM completion has no server-side side effect, so retrying the *same* request is safe.
We still generate an `idempotency_key` per logical request, thread it through, and log
it as the request id — this makes retries traceable and prepares for future
non-idempotent tool-calling (where the key would gate replay). We retry only 429/5xx;
4xx (bad request, auth, budget) fail immediately with no retry.

### What is explicitly NOT in Week 1

- Streaming wired into a UI (interface only this week)
- Retry-with-correction validation loop
- Circuit breaker / provider fallback (still Week 3+, unchanged from above)
- Shared cross-service package for the client (origination-only, unchanged)

### Deliverables this cycle

Per the assignment, the turn-in is three artifacts: **(1)** the client (the seven
modules above), **(2)** a code review of that client against the review checklist
(`docs/llm-client-code-review.md`), and **(3)** a prompt library (`app/prompts/`).
