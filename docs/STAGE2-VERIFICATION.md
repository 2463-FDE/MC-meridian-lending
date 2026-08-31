
# Stage 2: Verify Plan Against Spec

**Date:** 2026-07-01  
**Spec:** docs/specs/ai-assistant-week1.md  
**Plan:** docs/STAGE1-PLAN-AI-ASSISTANT.md  
**Status:** Verification in progress


---

## Acceptance Criteria Walk-Through

### D1: Production LLM Client Wrapper

| Acceptance | Spec Ref | Plan Coverage | Status |
|---|---|---|---|
| Client importable as `from app.llm_client import ClaudeClient` | §D1, AC#1 | 3.1, 3.2 | ✓ Covered |
| Timeout fires if Claude doesn't respond in 30s | §D1, AC#2 | 3.3, 3.5 | ✓ Covered |/usage

| 5xx retry with exponential backoff, max 3 attempts; 4xx fail immediately | §D1, AC#3 | 3.3, 3.6 | ✓ Covered |
| Structured output schema validation; malformed errors clear | §D1, AC#4 | 3.3, 3.7 | ✓ Covered |
| Token budget enforced per-request; over-budget refused with clear message | §D1, AC#5 | 3.3, 3.8 | ✓ Covered |
| Unit tests cover timeout, retry, validation, cost guard | §D1, AC#6 | 3.5–3.8 | ✓ Covered |
| No unit test logs contain PAN/CVV/SSN/email/phone | §D1, AC#7 | 3.9 | ✓ Covered |

**Summary:** D1 fully covered. All 7 acceptance criteria have plan items.

---

### D2: PCI/PII-Safe Logging

| Acceptance | Spec Ref | Plan Coverage | Status |
|---|---|---|---|
| PiiRedactor class with `redact(text: str) -> str` method | §D2, AC#1 | 2.1, 2.2 | ✓ Covered |
| Logging configured with redactor in all 7 services | §D2, AC#2 | 2.4–2.6 | ✓ Covered |
| Existing log files reviewed; any with PAN/CVV/full SSN flagged in debt-log | §D2, AC#3 | 1.5 (debt-log) | ⚠️ Partial |
| Unit tests verify PAN/CVV/full SSN/email/phone redacted | §D2, AC#4 | 2.3 | ✓ Covered |
| Partial SSN (last 4) preserved in logs | §D2, AC#5 | 2.2 | ✓ Covered |

**Summary:** D2 fully covered. AC#3 (review existing logs) is covered by the debt-log (1.5), which will flag any existing unredacted data. ✓

---

### D3: LOS↔LSS Seam Map

| Acceptance | Spec Ref | Plan Coverage | Status |
|---|---|---|---|
| Document exists (`docs/los-lss-seam.md`) | §D3, AC#1 | 1.4 | ✓ Covered |
| Clearly shows boarding flow (code + SQL reference) | §D3, AC#2 | 1.4 | ✓ Covered |
| Names servicing tables involved | §D3, AC#3 | 1.4 | ✓ Covered |
| Flags lack of async notification / event log | §D3, AC#4 | 1.4 | ✓ Covered |

**Summary:** D3 fully covered. Static analysis task (no code changes).

---

### D4: Debt-Log Entry

| Acceptance | Spec Ref | Plan Coverage | Status |
|---|---|---|---|
| `docs/debt-log.md` exists with header row and date-stamped entries | §D4, AC#1 | 1.5 | ✓ Covered |
| Each finding names file/line, risk, mitigation path | §D4, AC#2 | 1.5 | ✓ Covered |
| Links to relevant code (e.g., `services/payment-service/app/main.py:45`) | §D4, AC#3 | 1.5 | ✓ Covered |

**Summary:** D4 fully covered. Static documentation task.

---

### Process & Quality

| Acceptance | Spec Ref | Plan Coverage | Status |
|---|---|---|---|
| All work in feature branch off `main` | AC#8 | 1.1 | ✓ Covered |
| Commits with clear messages tracing to spec sections | AC#9 | 5.1–5.10 | ✓ Covered |
| ADR documents LLM client design | AC#10 | 5.8 | ✓ Covered |
| ADR documents logging redaction strategy | AC#11 | 5.9 | ✓ Covered |
| Unit tests pass for LLM client and redactor | AC#12 | 3.5–3.9, 2.3 | ✓ Covered |
| Integration test confirms logging redaction end-to-end | AC#13 | 2.7 | ✓ Covered |

**Summary:** All process criteria covered.

---

## Open Questions / Blocking Ambiguities

### Q1: Claude API Key & Authentication

**Status:** ⚠️ Clarification needed  
**Issue:** Spec does not specify where the Claude API key comes from (env var, config, secret manager).  
**Impact:** Blocking D1 implementation (ClaudeClient.__init__).  
**Options:**
- (A) Read from env var (e.g., `CLAUDE_API_KEY`) at runtime.
- (B) Injected via FastAPI dependency injection (app-level config).
- (C) Stored in a secrets manager (overkill for MVP).

**Recommendation:** Option A (env var, simplest for MVP). ClaudeClient.__init__ reads `os.getenv("CLAUDE_API_KEY")` and raises if missing.

**Decision needed:** Confirm Option A, or specify alternative.

---

### Q2: Claude Model ID

**Status:** ⚠️ Clarification needed  
**Issue:** Spec says "use the Claude SDK (latest model available)" but doesn't specify which model (claude-opus-4-8, claude-sonnet-5, claude-haiku-4-5, etc.).  
**Impact:** Affects cost, latency, capability tradeoffs.  
**Current guidance:** Per /claude-api skill, latest models are Fable 5, Opus 4.8, Sonnet 5, Haiku 4.5.

**Recommendation:** Use `claude-haiku-4-5-20251001` for MVP (cheapest, fast, appropriate for loan summarization). Upgrade to Sonnet/Opus if needed after Week 2.

**Decision needed:** Confirm Haiku 4.5, or specify a different model.

---

### Q3: Token Budget Defaults

**Status:** ⚠️ Clarification needed  
**Issue:** Spec says "cost guard (request-level token budget, refuse if over budget)" but doesn't specify the default budget value.  
**Impact:** Affects safety and usability of ClaudeClient.

**Recommendation:** Default to 10,000 tokens per request (rough estimate: ~7,500 tokens for a typical loan application + summary). Configurable at instantiation.

**Decision needed:** Confirm 10,000 token default, or specify a different value.

---

### Q4: Redaction Regex Patterns — Email & Phone

**Status:** ⚠️ Clarification needed  
**Issue:** Spec lists email and phone as fields to redact, but the loan-application data structure may not always include these. Should we redact them *if present*, or is it okay to only redact PAN/CVV/SSN if those are the only PII in the payload?

**Impact:** Scope of redaction patterns.

**Recommendation:** Implement patterns for all five (PAN, CVV, full SSN, email, phone), but test only on fields that actually appear in payment requests. For loan applications, email/phone may not appear in the redacted path; we redact them if they *do* appear.

**Decision needed:** Confirm all-five redaction, or narrow to PAN/CVV/SSN only if others don't appear in the payload.

---

### Q5: Existing Log Files — Archive or Deletion?

**Status:** ⚠️ Clarification needed  
**Issue:** Spec says "Existing log files are reviewed; any containing PAN/CVV/full SSN are flagged in the debt-log." It doesn't say what to *do* with those files.  
**Impact:** Security & compliance.

**Recommendation:** 
- (A) Leave logs/ in place, flag in debt-log, and handle deletion/archival as a separate security task (out of scope for Week 1).
- (B) Delete logs/ directory entirely (cleanest, but may lose debug data).

**Decision needed:** (A) or (B)?

---

### Q6: LOS↔LSS Seam Map — Scope

**Status:** ⚠️ Clarification needed  
**Issue:** "LOS↔LSS seam map" could mean:
- Minimal: just the boarding flow (intake → insert into servicing schema).
- Comprehensive: full data mapping (all fields, all tables, all constraints).

**Impact:** Effort (30 min vs. 2 hours).

**Recommendation:** Minimal for Week 1: boarding flow diagram + code/SQL references + list of gaps (no event log, no notification). Comprehensive mapping can be added in future.

**Decision needed:** Minimal or comprehensive?

---

## Summary: Blockers & Decisions Needed

| Blocker | Severity | Needed By |
|---|---|---|
| Q1: Claude API key source | High | Before 3.2 (ClaudeClient.__init__) |
| Q2: Claude model ID | High | Before 3.2 |
| Q3: Token budget default | Medium | Before 3.2, but can use placeholder (10k) for now |
| Q4: Redaction scope (email/phone) | Low | Before 2.2 (redactor), acceptable to include all five |
| Q5: Existing logs — archive vs. delete | Low | Before 1.5 (debt-log); recommend (A) out of scope |
| Q6: Seam map scope | Low | Before 1.4; recommend minimal |

---

## Stage 2 Gate: Ready to Proceed to Stage 3?

**Checklist:**

- [ ] All 4 deliverables (D1–D4) have plan coverage.
- [ ] All acceptance criteria are either covered by a plan item or explicitly deferred.
- [ ] Blocking ambiguities (Q1–Q6) are acknowledged and decisions are noted (even if deferred).
- [ ] No requirement is left ambiguous — every plan item traces to a spec criterion.

**Questions before lock:**

1. **Q1 (API key):** Confirm env var, or specify alternative?
2. **Q2 (Model ID):** Confirm Haiku 4.5, or specify different model?
3. **Q3 (Token budget):** Confirm 10,000 token default?
4. **Q4 (Redaction scope):** Include email/phone patterns (even if not in payload)?
5. **Q5 (Existing logs):** Leave in place & flag (deferred), or delete?
6. **Q6 (Seam map scope):** Minimal (boarding flow only) or comprehensive?

Once confirmed, we lock decisions as **ADRs (Stage 3)** and begin implementation.
