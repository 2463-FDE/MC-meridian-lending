# Spec: AI Assistant Infrastructure (Week 1)

**Owner:** Dana (VP Lending Ops)  
**Date:** 2026-07-01  
**Status:** Approved for build

---

## Executive Summary

Stand up a safe LLM client infrastructure for Meridian's loan origination platform. This is **not** a loan-summary feature yet; it's the hardened foundation that allows us to eventually wire LLM calls into a system that today logs PAN/CVV/SSN to plaintext files. Build the safe client, document the debt, and create a path forward.

---

## Problem Statement

**Surface request:** Dana wants the board to see "AI momentum" — a loan officer assistant that summarizes applications.

**Real problem:** The repo has serious PCI/security debt (hardcoded credentials, plaintext PAN/CVV in logs and schema, false README claims). Wiring an LLM into this system without hardening creates new liability: we'd be feeding the LLM sensitive data the system shouldn't be storing, and logging it further.

**This week's scope:** Build the safe *container* first. The loan-summary feature comes later, on top of this foundation.

---

## Deliverables (In Scope)

### D1. Production LLM Client Wrapper
A reusable, hardened HTTP client for Claude API calls that:
- Enforces a timeout (30s, configurable)
- Implements automatic retry (exponential backoff, max 3 attempts)
- Enforces structured output (JSON schema validation)
- Implements cost guard (request-level token budget, refuse if over budget)
- Handles HTTP errors gracefully (4xx → raise, 5xx → retry)
- Logs API calls and responses **without exposing PII/PAN/CVV/SSN**

**Acceptance:**
1. Client is importable as `from app.llm_client import ClaudeClient`
2. Timeout fires if Claude doesn't respond in 30s
3. 5xx errors trigger exponential backoff (base 2, max 3 retries); 4xx errors fail immediately
4. Structured output schema is validated; malformed responses raise a clear error
5. Token budget is enforced per-request; requests exceeding budget are refused with a clear message
6. Unit tests cover timeout, retry logic, validation, and cost guard
7. No unit test logs contain PAN/CVV/SSN/email/phone

### D2. PCI/PII-Safe Logging
Configure logging across the Meridian services (origination-service, payment-service, servicing-service, decision-service, disclosure-service, kyc-service) so that:
- No PAN, CVV, full SSN, or email is ever written to `logs/`
- Partial SSN (last 4 digits only) is allowed in logs for audit trails
- PII in request/response bodies is redacted before logging
- The redacting filter is reusable and tested in isolation

**Acceptance:**
1. A `PiiRedactor` class (or similar) exists in a shared module with a `redact(text: str) -> str` method
2. Logging is configured with this redactor in all services
3. Existing log files are reviewed; any containing PAN/CVV/full SSN are flagged in the debt-log
4. Unit tests verify PAN ("4111…"), CVV ("123"), full SSN ("412-55-9981"), email, and phone are redacted
5. Partial SSN (last 4) is preserved: "SSN: •••-••-1234" or similar

### D3. LOS↔LSS Seam Map
A visual (ASCII or diagram) documenting how funded loans flow from origination (LOS) to servicing (LSS):
- Entry point: origination-service's intake flow
- The direct DB insert that boards to servicing
- Table columns involved (origination side, servicing side)
- Data fields that cross the seam
- Current gaps (e.g., no event log, no async notification)

**Acceptance:**
1. A document (`docs/los-lss-seam.md` or `.txt`) exists
2. It clearly shows the boarding flow (code reference + SQL)
3. It names the servicing tables involved
4. It flags the current lack of async notification / event log

### D4. Debt-Log Entry
A new entry in `docs/debt-log.md` (create if missing) documenting the security/compliance findings discovered:

**Findings to document:**
- **D1 (Hardcoded credentials):** Bureau keys and processor keys in `config.py` and `.env` (EXPERIAN_KEY, CORE_BANKING_API_KEY, card-processor key). Consequence: repo leak = live credential leak. Mitigation path: move to sealed env vars or secret manager before prod.
- **D5 (Plaintext PAN/CVV/SSN in logs):** payment-service.log contains full cardholder data in request bodies. Consequence: violates PCI-DSS 3.4 (encrypted storage/transmission); log files are backup targets. Mitigation: D2 (PII-safe logging) addresses this going forward.
- **D13 (PAN/CVV in schema):** payments table has direct `pan` and `cvv` columns. Consequence: violates PCI-DSS; if DB is breached, full cardholder data is exposed. Mitigation: future work (tokenization / separate key-value store); for now, document and flag in code review.
- **D2 (Float money math):** balance.py uses `float(amount)` for balance calculations. Consequence: rounding errors compound; retries can double-charge. Mitigation: future migration to Decimal; flag all money writes in code review.

**Acceptance:**
1. `docs/debt-log.md` exists with a header row and date-stamped entries
2. Each finding names the file/line, the risk, and the mitigation path
3. Links to relevant code (e.g., `services/payment-service/app/main.py:45`)

---

## Out of Scope (Not This Week)

- Building the actual loan-summary AI feature (deferred until safe infrastructure exists)
- Migrating payments to tokenized card storage (future work, listed in debt-log)
- Replacing float with Decimal for money math (future work, listed in debt-log)
- Rotating or moving hardcoded credentials (future work, flagged in debt-log as D1)
- Form UI polish (mentioned in Dana's request but not critical for board momentum)

---

## Acceptance Criteria (To Verify at End of Week)

### Functional
1. LLM client wrapper exists, is tested, and can make a real Claude API call with timeout/retry/validation
2. Logging across all services is configured to redact PAN/CVV/full SSN
3. LOS↔LSS seam map is documented
4. Debt-log entry is committed with all four findings (D1, D5, D13, D2)

### Security/Compliance
5. No PAN/CVV/full SSN appears in any unit test logs
6. The LLM client's logging never exposes customer data
7. Code review comments flag the existing debt (D1, D5, D13) and note that D2/D4 address logging going forward

### Process
8. All work is in a feature branch off `main`
9. All changes are committed with clear messages tracing to spec sections
10. An ADR documents the LLM client design (timeout/retry/validation strategy)
11. An ADR documents the logging redaction strategy
12. Unit tests pass for LLM client and redactor
13. Integration test confirms logging redaction works end-to-end

---

## Notes for Implementation

- **LLM client:** Use the Claude SDK (latest model available; check /claude-api for current pricing/model IDs)
- **Logging redaction:** Regex-based redaction is acceptable for MVP (PAN = `\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}`, etc.)
- **Debt-log:** Use a simple Markdown table with columns: ID, Finding, Risk, File:Line, Mitigation Path, Date
- **Test strategy:** Unit test the redactor and LLM client in isolation; integration test that redaction works when a service logs a payment request

---

## Success Metrics

- Dana sees a week-1 merge with LLM infrastructure ready (not the feature itself, but the foundation)
- Security/compliance gets a debt-log showing we're aware of the issues and building safe practices
- Next week: loan-summary feature built on top of D1 (safe client) + D2 (safe logging)
