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
   - Include `response_schema` as JSON schema constraint if provided (native Claude structured output).

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

### Native Claude Structured Output
- **Why:** Claude API (Anthropic SDK) has built-in constraints for structured output. Using native support is safest and avoids reimplementing validation.
- **Fallback:** If SDK doesn't support it, use Pydantic model + manual validation (origination-service already uses Pydantic 2.10.4).

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
