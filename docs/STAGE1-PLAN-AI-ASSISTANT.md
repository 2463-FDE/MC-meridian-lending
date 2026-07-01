# Stage 1 Plan: AI Assistant Infrastructure (Week 1)

**Date:** 2026-07-01  
**Spec:** docs/spec-ai-assistant-week1.md  
**Status:** Ready for gate approval

---

## Gap Analysis

### D1: LLM Client Wrapper

**Current State:**
- `services/origination-service/app/clients.py` exists: a basic httpx wrapper for inter-service calls (KYC, decision, disclosure).
- No LLM-specific client.
- `httpx==0.28.1` already in origination-service/requirements.txt.
- No Anthropic SDK dependency in any service.

**Gap:**
- Need a new `llm_client.py` module with a `ClaudeClient` class (importable as `from app.llm_client import ClaudeClient`).
- Requires Anthropic SDK (`anthropic>=1.0.0` or latest).
- Must implement: timeout (30s), exponential backoff retry (max 3), structured JSON validation, token-budget cost guard.
- Must NOT log PII (deferred to D2, but must use the redactor when logging).

**Scope:**
- Add Anthropic SDK to requirements.txt.
- Create `app/llm_client.py` in origination-service (or shared if multiple services use it; for MVP, origination is sufficient).
- Unit tests for timeout, retry, validation, cost guard.

---

### D2: PCI/PII-Safe Logging

**Current State:**
- `services/origination-service/app/logging_config.py`: "Logs the full request body on every POST — including PII. No redaction." (D5)
- `services/payment-service/app/logging_config.py`: "writes the full charge request body (PAN, CVV, SSN) at INFO. No redaction." (D5, #7)
- All other services (`kyc-service`, `decision-service`, `disclosure-service`, `servicing-service`, `gateway`) have similar logging_config.py patterns.
- No redaction layer exists anywhere.

**Gap:**
- Need a `PiiRedactor` class (or utility module) that redacts PAN, CVV, full SSN, email, phone from text.
- Apply redactor before logging in ALL services.
- Preserve partial SSN (last 4 digits) for audit trails.
- Unit tests for regex patterns (PAN, CVV, full SSN, email, phone).
- Integration test: verify a payment-service request with PAN/CVV is redacted in logs.

**Scope:**
- Create a shared redaction utility (location TBD: shared module or lib/ or each service gets a copy).
- Update logging_config.py in each of the 7 services to apply redaction.
- Add unit tests for redactor.
- Add integration test verifying payment-service logs are redacted.

---

### D3: LOS↔LSS Seam Map

**Current State:**
- `docs/architecture.md` describes the seam at a high level: "LOS→LSS boarding. A funded loan is 'boarded' by a direct cross-schema INSERT from origination-service/app/intake.py::board_to_servicing into servicing's loans/balances — no API or event between the domains."
- No isolated diagram or detailed flow.

**Gap:**
- Create a new `docs/los-lss-seam.md` document with:
  - Visual (ASCII or prose) of the boarding flow.
  - Code references (origination-service/app/intake.py::board_to_servicing line number).
  - SQL references (servicing schema inserts).
  - Data fields that cross the seam.
  - Current gaps (no async event, no notification).

**Scope:**
- Read `origination-service/app/intake.py` to understand boarding logic.
- Read DB schema to identify tables/columns involved.
- Write `docs/los-lss-seam.md`.
- No code changes required.

---

### D4: Debt-Log Entry

**Current State:**
- ADR 0003 documents D13 (PAN/CVV storage) and D2 (float money math).
- ADR 0004 and others describe decomposition but don't centralize debt.
- No standalone debt-log file.

**Gap:**
- Create `docs/debt-log.md` with a table/list of known findings.
- Document D1 (hardcoded credentials), D5 (plaintext PAN/CVV in logs), D13 (PAN/CVV in schema), D2 (float money math).
- Each entry: ID, Finding, Risk, File:Line, Mitigation Path, Date.

**Scope:**
- Grep for hardcoded keys in config.py and .env.
- Review existing ADRs and translate to debt entries.
- Write `docs/debt-log.md`.
- No code changes required.

---

## Decision Ledger

### D1.1: Where to build the LLM client?

**Options:**
1. **Shared module in a new `lib/` directory** — all services import from there.
   - Pro: DRY, one source of truth, easy to test.
   - Con: Requires shared packaging/distribution, adds complexity.
2. **In origination-service only** — put `llm_client.py` in origination-service/app/.
   - Pro: Simplest, no shared infra, origination is the primary consumer.
   - Con: If payment-service or others need to call LLM later, we duplicate code.
3. **New lightweight `llm-service`** — dedicated microservice for LLM calls.
   - Pro: Clean separation, scalable.
   - Con: Overkill for Week 1, adds ops/deployment complexity.

**Decision:** **Option 2 (origination-service only).**

**Why:** Spec says the loan-summary feature is Week 1, which lives in origination. Starting with origination-service keeps us focused and minimal. If payment-service or others need LLM later, we can refactor to shared lib then (YAGNI: You Aren't Gonna Need It yet). origination-service is the sole consumer for Week 1.

---

### D1.2: Structured output validation approach?

**Options:**
1. **Pydantic model + validation** — define a schema (e.g., `SummaryResponse`), validate response as Pydantic, raise on mismatch.
   - Pro: Type-safe, integrates with FastAPI, clear contract.
   - Con: Requires schema definition upfront.
2. **Raw JSON + jsonschema validation** — use jsonschema library, validate against a JSON Schema dict.
   - Pro: More flexible, decoupled from Pydantic.
   - Con: Less type-safe, harder to enforce in code.
3. **Claude's native structured output** — use Claude API's built-in constraint (if available in Anthropic SDK).
   - Pro: Best compatibility, native support.
   - Con: Depends on SDK version; verify availability first.

**Decision:** **Option 3 (Claude's native structured output)**, falling back to **Option 1 (Pydantic)** if the SDK doesn't support it.

**Why:** Claude API (especially newer models) has native structured-output constraints. Using it is safest and avoids reimplementing validation. If unavailable, Pydantic is our MVP (origination-service already uses Pydantic 2.10.4).

---

### D1.3: Where does PII redaction happen in the LLM client?

**Options:**
1. **In ClaudeClient.call() before sending** — redact the request payload before posting to Claude.
   - Pro: Prevents PII from ever leaving the system.
   - Con: Requires the client to know about PII patterns; couples client to redaction logic.
2. **In the caller (origination-service endpoint)** — redact before calling ClaudeClient.
   - Pro: Clear separation of concerns; endpoint controls what's sent.
   - Con: Caller must remember to redact; easy to forget.
3. **In logging only** — send unredacted to Claude, but redact in logs.
   - Pro: Simplest for the client.
   - Con: Claude API receives PII; if API is hacked, PII is compromised. Not compliant with privacy-first design.

**Decision:** **Option 1 (redact in ClaudeClient.call() before sending).**

**Why:** Spec says "LLM client's logging never exposes customer data." Stronger: client should never *send* unredacted PII to Claude API. Redacting at the boundary (client.call()) ensures it happens once, universally. Caller is responsible for *selecting* what data to send, client is responsible for *sanitizing* it before sending.

---

### D2.1: Where to implement the PiiRedactor?

**Options:**
1. **Shared module in each service** — copy/paste PiiRedactor into each service's app/ (origination, payment, kyc, decision, disclosure, servicing, gateway).
   - Pro: No dependencies, self-contained per service.
   - Con: Duplicated code; if pattern needs to change, 7 places to update.
2. **New `shared_lib/` or `lib/` directory at repo root** — import from there.
   - Pro: DRY, one source.
   - Con: Requires setup.py or similar for distribution.
3. **In one service, imported by others via direct module import** — e.g., from origination_service.app.redactor import PiiRedactor.
   - Pro: Works without packaging infrastructure.
   - Con: Creates coupling between services; fragile if services run in different containers.

**Decision:** **Option 1 (shared module copied to each service, initially).**

**Why:** Week 1 is about *introducing* the redaction pattern and proving it works. Sharing code via copy is acceptable for MVP and lets each service own its logging independently. If redaction patterns need to change, the risk of missed updates is real but manageable for 7 services. In Week 2, we can move to a shared lib if the pattern stabilizes. (This is a pragmatic tradeoff: correctness over DRY in Week 1.)

---

### D2.2: Which fields to redact?

**Options:**
1. **Conservative:** PAN, CVV, full SSN, email, phone, all account numbers.
2. **Minimal:** PAN, CVV, full SSN only.
3. **Aggressive:** everything that could be PII, including names, addresses, dates of birth.

**Decision:** **Option 1 (Conservative).**

**Why:** Spec explicitly lists PAN/CVV/SSN/email/phone. We should match the spec exactly. Conservative beats minimal; minimal beats aggressive (aggressive can cause log entries to become useless for debugging).

---

## Implementation Plan

### Phase 1: Prepare & Foundations (Day 1 – AM)

| Item | Task | Dependencies | File(s) |
|------|------|---|---|
| 1.1 | Create feature branch off `main` | None | git branch `feature/llm-foundation-week1` |
| 1.2 | Add Anthropic SDK to origination-service/requirements.txt | None | `services/origination-service/requirements.txt` |
| 1.3 | Verify Anthropic SDK is available (run `pip install anthropic`) | 1.2 | — |
| 1.4 | Create `docs/los-lss-seam.md` (static analysis, no code) | None | `docs/los-lss-seam.md` |
| 1.5 | Create `docs/debt-log.md` with D1/D5/D13/D2 entries | None | `docs/debt-log.md` |

**Trace to spec:**
- 1.2: D1 (LLM client)
- 1.4: D3 (seam map)
- 1.5: D4 (debt-log)

---

### Phase 2: PII Redactor (Day 1 – PM)

| Item | Task | Dependencies | File(s) |
|------|------|---|---|
| 2.1 | Create shared redactor module | None | `services/origination-service/app/redactor.py` (+ copy to other services) |
| 2.2 | Implement PiiRedactor.redact(text) with regex patterns (PAN, CVV, full SSN, email, phone) | 2.1 | `services/*/app/redactor.py` |
| 2.3 | Unit test PiiRedactor (test each pattern) | 2.1, 2.2 | `services/origination-service/tests/test_redactor.py` |
| 2.4 | Update logging_config.py in origination-service to use redactor | 2.1, 2.2, 2.3 | `services/origination-service/app/logging_config.py` |
| 2.5 | Update logging_config.py in payment-service to use redactor | 2.1, 2.2, 2.3 | `services/payment-service/app/logging_config.py` |
| 2.6 | Update logging_config.py in remaining 5 services (kyc, decision, disclosure, servicing, gateway) | 2.1, 2.2, 2.3 | `services/{kyc,decision,disclosure,servicing,gateway}-*/app/logging_config.py` |
| 2.7 | Integration test: payment-service POST with PAN/CVV → verify logs are redacted | 2.4, 2.5, 2.6 | `services/payment-service/tests/test_logging_redaction.py` |

**Trace to spec:**
- 2.1–2.7: D2 (PII-safe logging)

---

### Phase 3: LLM Client Wrapper (Day 2 – AM/PM)

| Item | Task | Dependencies | File(s) |
|------|------|---|---|
| 3.1 | Create `app/llm_client.py` with `ClaudeClient` class | 1.2 | `services/origination-service/app/llm_client.py` |
| 3.2 | Implement `ClaudeClient.__init__(api_key, model, timeout=30, max_retries=3, token_budget=10000)` | 3.1 | same |
| 3.3 | Implement `ClaudeClient.call(messages, response_schema=None)` with: timeout enforcement, exponential backoff retry, structured output validation, cost guard (token budget check) | 3.1, 3.2 | same |
| 3.4 | Implement logging in ClaudeClient that uses the redactor (D2) | 3.3, 2.1 | same |
| 3.5 | Unit test timeout: verify httpx timeout fires after 30s | 3.1–3.4 | `services/origination-service/tests/test_llm_client.py` |
| 3.6 | Unit test retry: mock 5xx, verify exponential backoff + max 3 retries | 3.1–3.4 | same |
| 3.7 | Unit test validation: mock malformed JSON, verify error raised | 3.1–3.4 | same |
| 3.8 | Unit test cost guard: verify request exceeding token budget is refused | 3.1–3.4 | same |
| 3.9 | Unit test logging: verify no PAN/CVV/SSN in any test logs | 3.1–3.4, 2.1 | same |

**Trace to spec:**
- 3.1–3.9: D1 (LLM client wrapper)

---

### Phase 4: Smoke Test & Verification (Day 2 – afternoon/Day 3 – AM)

| Item | Task | Dependencies | File(s) |
|------|------|---|---|
| 4.1 | Start the live stack: `make up` | All prior | — |
| 4.2 | Verify origination-service container health | 4.1 | — |
| 4.3 | Smoke test ClaudeClient: make a real Claude API call (with valid API key from env) | 3.1–3.9, 4.1, 4.2 | Manual test or pytest fixture |
| 4.4 | Verify logs are produced and redacted (check `logs/origination-service.log`) | 4.3 | — |
| 4.5 | Run full test suite: `cd services/origination-service && python -m pytest` | All prior | — |
| 4.6 | Run payment-service redaction integration test | 2.7, 4.1 | — |

**Trace to spec:**
- 4.1–4.6: Acceptance criteria #5–7 (security/compliance)

---

### Phase 5: Commits & ADRs (Day 3 – AM/afternoon)

| Item | Task | Dependencies | Commit Message |
|------|------|---|---|
| 5.1 | Commit: Add Anthropic SDK to origination-service | 1.2 | `chore: add anthropic sdk to origination-service` |
| 5.2 | Commit: Create PiiRedactor + tests | 2.1–2.3 | `feat: implement PII redactor for logging (D2)` |
| 5.3 | Commit: Update logging in origination + payment + other 5 services | 2.4–2.6 | `feat: apply PII redaction to all service logging (D2)` |
| 5.4 | Commit: Add redaction integration test | 2.7 | `test: verify logging redaction works end-to-end (D2)` |
| 5.5 | Commit: Create LLM client + unit tests | 3.1–3.9 | `feat: implement Claude LLM client with timeout/retry/validation/cost-guard (D1)` |
| 5.6 | Commit: Create los-lss seam map | 1.4 | `docs: add LOS-LSS seam map (D3)` |
| 5.7 | Commit: Create debt-log | 1.5 | `docs: add debt-log documenting D1/D5/D13/D2 (D4)` |
| 5.8 | Write ADR 0005: LLM Client Design | 3.1–3.9 | — (as part of 5.5) |
| 5.9 | Write ADR 0006: Logging Redaction Strategy | 2.1–2.7 | — (as part of 5.3) |
| 5.10 | Commit: Add ADR 0005 + 0006 | 5.8, 5.9 | `docs: lock LLM client + logging redaction strategy as ADR 0005/0006` |

**Trace to spec:**
- 5.1–5.10: Acceptance criteria #9–10 (commits, ADRs)

---

### Phase 6: Documentation & Readiness (Day 3 – afternoon)

| Item | Task | Dependencies | File(s) |
|------|------|---|---|
| 6.1 | Update ARCHITECTURE.md to reference LLM client and redactor | 3.1, 2.1 | `ARCHITECTURE.md` (1-2 lines) |
| 6.2 | Update README.md to note Week 1 deliverables (if intended for user-facing) | All prior | `README.md` (optional) |
| 6.3 | Verify branch has all 4 deliverables (D1, D2, D3, D4) | All prior | — |
| 6.4 | Generate PR-ready summary (branch name, PRs, test results) | 4.5, 5.10 | — (for Stage 9) |

**Trace to spec:**
- 6.1–6.4: Acceptance criteria #1–13

---

## Requirement Traceability

| Spec Acceptance Criterion | Plan Items | Status |
|---|---|---|
| (1) LLM client importable as `from app.llm_client import ClaudeClient` | 3.1, 3.2 | ✓ Planned |
| (2) Timeout fires after 30s | 3.3, 3.5 | ✓ Planned |
| (3) 5xx retry with exponential backoff, max 3 attempts | 3.3, 3.6 | ✓ Planned |
| (4) Structured output schema validation, malformed errors clear | 3.3, 3.7 | ✓ Planned |
| (5) Token budget enforced, over-budget requests refused | 3.3, 3.8 | ✓ Planned |
| (6) Unit tests cover timeout/retry/validation/cost-guard | 3.5–3.8 | ✓ Planned |
| (7) No PAN/CVV/SSN in unit test logs | 3.9 | ✓ Planned |
| (8) PiiRedactor class with redact(text) method, reusable | 2.1, 2.2 | ✓ Planned |
| (9) Logging configured with redactor in all 7 services | 2.4–2.6 | ✓ Planned |
| (10) Existing logs flagged if they contain PAN/CVV/SSN (debt-log) | 1.5 | ✓ Planned |
| (11) Unit tests verify PAN/CVV/full SSN/email/phone redacted | 2.3 | ✓ Planned |
| (12) Partial SSN (last 4) preserved in logs | 2.2 | ✓ Planned |
| (13) LOS↔LSS seam map documented | 1.4 | ✓ Planned |
| (14) Debt-log entry with D1/D5/D13/D2 findings (file:line, mitigation path) | 1.5 | ✓ Planned |
| (15) LLM client tested with real Claude API call (smoke) | 4.3 | ✓ Planned |
| (16) All work in feature branch off main | 1.1 | ✓ Planned |
| (17) ADRs document LLM client design + logging redaction strategy | 5.8, 5.9 | ✓ Planned |
| (18) Unit tests pass for LLM client + redactor | 3.5–3.9, 2.3 | ✓ Planned |
| (19) Integration test confirms logging redaction end-to-end | 2.7 | ✓ Planned |

---

## Summary: What Will Be Shipped

**By end of Week 1:**

1. **Feature branch `feature/llm-foundation-week1`** with clean commit history.
2. **`services/origination-service/app/llm_client.py`** — production LLM client (timeout, retry, validation, cost guard).
3. **`services/*/app/redactor.py`** (7 copies) — PII redaction utility applied to all services' logging.
4. **Updated `services/*/app/logging_config.py`** (7 services) — using redactor to sanitize logs.
5. **`docs/los-lss-seam.md`** — visual/prose map of loan boarding flow.
6. **`docs/debt-log.md`** — centralized tracking of D1/D5/D13/D2 findings.
7. **ADR 0005** (LLM Client Design) and **ADR 0006** (Logging Redaction Strategy) — locked decisions.
8. **Test suite:**
   - Unit tests for LLM client (timeout, retry, validation, cost guard).
   - Unit tests for PiiRedactor.
   - Integration test for end-to-end logging redaction.
   - All tests pass, no PII in test logs.
9. **Smoke test against live stack** — real Claude API call works, logs are redacted.

**NOT shipped:** The loan-summary feature (deferred to Week 2). This week is pure infrastructure.

---

## Decision Gate: Ready to Proceed?

This plan is complete when you confirm:

- [ ] Gap analysis is accurate (D1–D4 gaps match your understanding).
- [ ] Decision ledger rationale makes sense (LLM in origination-service, redaction per-service MVP, etc.).
- [ ] Implementation plan is feasible in 3 days (Days 1–3 of the week).
- [ ] Traceability is clear (every spec acceptance criterion is covered).

**Questions for clarification before we lock it and proceed to Stage 2?**
