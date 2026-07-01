# ADR 0006: PII Redaction for Logging

- **Status:** Accepted
- **Date:** 2026-07-01
- **Author:** Claude Code

---

## Context

Meridian's current logging explicitly documents PII: payment-service logs full PAN/CVV/SSN in request bodies, origination-service logs full request payloads. This violates PCI-DSS 3.4 (encrypted storage/transmission) and creates liability if log files are breached or left in backups.

The LLM client (ADR 0005) requires safe logging to avoid compounding the debt. More broadly, all 7 services should redact PII before writing to disk.

We need a reusable, tested redaction strategy that can be applied across all services.

---

## Decision

We will implement a `PiiRedactor` class that redacts PII patterns from text before logging, and apply it to logging configuration in all 7 Meridian services.

### 1. PiiRedactor Implementation

**Location:** `services/origination-service/app/redactor.py` (original), then copied to each of the 6 other services.

**Interface:**
```python
class PiiRedactor:
    """Redacts PII patterns from text before logging."""
    
    @staticmethod
    def redact(text: str) -> str:
        """Redact PII from text. Return redacted copy."""
```

**Redaction Patterns:**

| PII Type | Pattern | Redacted Form | Example |
|---|---|---|---|
| **PAN (Visa/MC/Amex)** | `\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}` | `••••-••••-••••-1234` | `4111-1111-1111-1111` → `••••-••••-••••-1111` |
| **CVV** | `"cvv":\s*"?(\d{3,4})"?` (JSON) or `\b\d{3,4}\b` in card context | `••••` | `"cvv": "123"` → `"cvv": "••••"` |
| **Full SSN** | `\b\d{3}-\d{2}-\d{4}\b` (XXX-XX-XXXX) | `•••-••-LAST4` | `412-55-9981` → `•••-••-9981` |
| **Email** | `[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-z]{2,}` | `••••@••••••.com` | `user@example.com` → `••••@•••••••.com` |
| **Phone** | `\b\d{3}[-.]?\d{3}[-.]?\d{4}\b` | `•••-•••-LAST4` | `555-123-4567` → `•••-•••-4567` |

**Partial SSN preservation:** Last 4 digits of SSN are preserved for audit trails (allows officers to match "last 4 of SSN" without exposing full).

### 2. Integration with Logging

**In each service's `logging_config.py`:**

```python
from app.redactor import PiiRedactor

class RedactingFormatter(logging.Formatter):
    """Custom formatter that redacts PII before writing."""
    
    def format(self, record: logging.LogRecord) -> str:
        # Format the message normally
        msg = super().format(record)
        # Redact PII
        return PiiRedactor.redact(msg)

def get_logger(name: str) -> logging.Logger:
    # ... existing setup ...
    fmt = RedactingFormatter("%(levelname)s %(asctime)s %(name)s %(message)s")
    # Apply to all handlers (console + file)
```

**Services to update:**
1. origination-service (create redactor here)
2. payment-service (highest risk; currently logs full PAN/CVV/SSN)
3. servicing-service
4. decision-service
5. disclosure-service
6. kyc-service
7. gateway

### 3. Existing Logs

**Handling:** Existing log files (in `logs/`) that contain unredacted PAN/CVV/SSN are flagged in `docs/debt-log.md` (D5 finding). No deletion; archival/rotation is a separate security task (out of scope for week 1).

---

## Rationale

### Why Regex Patterns?

- **Simplicity:** No database lookup, no external API needed. Runs inline in logger.
- **Performance:** Regex is fast (~1ms per log entry). Acceptable for INFO/DEBUG logging.
- **Maintainability:** Patterns are centralized in one class. Easy to update if new PII types emerge.

### Why Copy to Each Service (MVP)?

- **Pro:** No shared infrastructure; each service owns its redaction independently.
- **Con:** Code duplication across 7 services.
- **Future:** Week 2+ can migrate to shared lib if patterns stabilize.

### Why Custom RedactingFormatter?

- **Why:** Formatter is where all log text is serialized. Redacting at the formatter level ensures ALL logging paths (console, file, structured) apply redaction without caller awareness.
- **Alternative:** Redact at caller (e.g., before calling `log.info()`). Rejected: easy to forget, scattered across codebase.

### Why Preserve Last 4 of SSN?

- **Why:** Loan officers need to match "customer's last 4 SSN" in calls. Full SSN is never needed in logs.
- **Tradeoff:** Last 4 alone is weak PII (not unique), so OK to preserve for debugging.

### Why All Five PII Types?

- **Why:** Spec lists all five. Conservative coverage: redact if present, even if a particular service doesn't typically see all five (e.g., gateway may not see card data directly).
- **Benefit:** Future-proofed; if data flow changes, redaction is still safe.

---

## Consequences

### Positive
- **Compliance:** Logs never contain plaintext PAN/CVV/full SSN. Satisfies PCI-DSS 3.4 (no plaintext storage).
- **Auditability:** Redacted logs still allow debugging ("app processed payment for SSN •••-••-1234"). Partial SSN helps match customer context.
- **Safe by default:** Once deployed, all new logs are redacted without code changes required in business logic.
- **Testable:** PiiRedactor is tested in isolation; integration test verifies redaction works end-to-end.

### Negative
- **Redaction overhead:** Regex evaluation on every log entry (~1ms per entry). Negligible for typical logging volume (100s/sec), but worth monitoring if logging becomes higher-volume.
- **False negatives:** Unusual PII formats (e.g., international phone, email with non-standard TLD) might not match. Mitigated by conservative patterns and test coverage.
- **Code duplication:** 7 copies of `redactor.py`. Acceptable MVP; refactor to shared lib in week 2+ if patterns stabilize.

### Future Work
- **Week 2+:** Extract PiiRedactor to shared module (`services/lib/redactor.py` or similar).
- **Week 2+:** Add structured logging (JSON) with redaction applied to field values.
- **Week 3+:** Centralize log collection with Loki/ELK to ensure redaction is enforced at all tiers.

---

## Testing Strategy

### Unit Tests (PiiRedactor)
- Test each pattern (PAN, CVV, full SSN, email, phone) with known examples.
- Verify partial SSN (last 4) is preserved.
- Verify multiple PII instances in one string are all redacted.
- Verify non-PII text is unchanged.

### Integration Test (payment-service)
- Create a test payment request with PAN, CVV, SSN.
- Log the request (with redaction enabled).
- Read the log file.
- Verify PAN, CVV, full SSN are redacted; partial SSN is preserved.
- Verify no plaintext sensitive data in logs.

### Smoke Test (live stack)
- Bring up services with redaction enabled.
- Process a payment through the full stack.
- Check logs across all services.
- Verify PII is consistently redacted.

---

## Compliance Notes

- **PCI-DSS 3.4:** "Rendering PAN unreadable anywhere it is stored (including on portable digital media, backup media, and in logs)." Redaction via regex achieves this.
- **Data Minimization:** Logs now contain only what's needed for debugging, not full cardholder data.
- **Auditability:** Redacted logs still allow matching customers (via last 4 SSN) and auditing actions.

---

## Debt Logged

This ADR addresses **D5 (Plaintext PAN/CVV/SSN in logs)** from the debt-log. Existing unredacted logs are flagged as debt; redaction prevents future occurrences.

**Related:** ADR 0005 (LLM Client) depends on this redaction strategy to ensure safe logging of LLM calls.
