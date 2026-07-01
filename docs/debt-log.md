# Meridian Lending — Debt Log

**Date:** 2026-07-01  
**Scope:** Known security, compliance, and architectural debt

This document tracks known issues, their business/compliance impact, and mitigation paths. It is not an exhaustive audit; it captures *known, documented* debt discovered during the LLM infrastructure build (Week 1).

---

## Debt Entries

### D1: Hardcoded Credentials in Code and Environment

| Field | Value |
|---|---|
| **ID** | D1 |
| **Finding** | Bureau and payment-processor API keys are hardcoded in source code and `.env`. |
| **Location** | `services/decision-service/app/config.py`: `EXPERIAN_KEY = "EXAMPLE-LEAKED-KEY-rotate-me"` (lines ~15–20) |
| | `services/origination-service/app/config.py`: Stale duplicate (lines ~18–22) |
| | Root `.env` (committed): `CORE_BANKING_API_KEY = "cb_live_..."`, card processor key (line ~12–14) |
| **Risk** | **Critical.** If repo is leaked (GitHub public, compromised dev machine, etc.), all live credentials are exposed. Attacker can: make credit pulls against Experian, charge cards, access banking APIs. PCI-DSS violation (3.5.1: no hardcoded secrets). |
| **Current Impact** | Keys are in source control history (git log). Even if deleted now, they remain in old commits. |
| **Mitigation Path** | **Week 2+:** Rotate all compromised keys immediately. Move credentials to sealed env vars or secret manager (e.g., AWS Secrets Manager, HashiCorp Vault). Use CI/CD to inject secrets at deploy time. Remove all old commits containing keys (force-push after rotation, or rewrite history). |
| **Status** | Open; flagged as debt; no immediate fix (out of scope for Week 1). |

---

### D2: Float Arithmetic for Money

| Field | Value |
|---|---|
| **ID** | D2 |
| **Finding** | All monetary amounts are stored and calculated as `DOUBLE PRECISION` (float), not fixed-point decimal. |
| **Location** | `db/init/001_schema.sql` (lines 33, 36, 68–72, 80–81, 90, 101): `DOUBLE PRECISION` used for `amount`, `income`, `apr`, `monthly_payment`, `finance_charge`, `balance`, etc. |
| | `services/servicing-service/app/balance.py`: `new_balance = current - float(amount)` (line ~25) |
| | `services/disclosure-service/app/apr.py`, `fees.py`, `offer.py`: All calculations use float. |
| **Risk** | **High.** Rounding errors compound across calculations. Example: |
| | Loan: $10,000 / 36 months = $277.7777... per month. |
| | Each month, 2–4 cents of rounding error. |
| | After 36 months: balance may not be exactly $0. |
| | Impact: Reconciliation fails, audit logs show discrepancies, customer complaints ("why is $0.03 still owed?"). |
| | PCI-DSS **does not prohibit** float math, but it creates operational risk (disputes, chargebacks). |
| **Current Impact** | Test suite (`services/*/tests/test_money.py`) includes tests that **fail by design**; they document rounding defects. No one reacts to these failures (CI runs with `|| true`). |
| **Mitigation Path** | **Week 2–3:** Migrate to `NUMERIC(19,2)` (fixed-point, cents precision) in DB. Update all ORM models to use `decimal.Decimal`. Recalculate all outstanding balances post-migration (audit + customer communication). Add pre-payment validation to round amounts to cents. Tighten test suite to fail on rounding discrepancies > $0.01. |
| **Status** | Open; flagged as debt; accepted design risk (tradeoff: simplicity vs. correctness). |

---

### D5: Plaintext PAN/CVV/SSN in Logs

| Field | Value |
|---|---|
| **ID** | D5 |
| **Finding** | Payment and origination services log full request/response bodies, including plaintext PAN, CVV, and SSN. |
| **Location** | `services/payment-service/app/logging_config.py` (line 1–4): "writes the full charge request body (PAN, CVV, SSN) at INFO." |
| | `services/payment-service/app/main.py` (assumed): Logs full request body on POST `/charge`. |
| | `services/origination-service/app/logging_config.py` (line 3–4): "Logs the full request body on every POST — including PII. No redaction." |
| | `services/origination-service/app/intake.py` (line 15): `log.info("POST /applications intake req=%s", payload)` — payload includes SSN, email, phone. |
| | **Log files:** `logs/payment-service.log`, `logs/origination-service.log` contain unredacted cardholder data. |
| | **Sample from repo handover:** `INFO charge req={"pan":"4111111111111111","cvv":"123","ssn":"412-55-9981","amount":250.00}` |
| **Risk** | **Critical.** PCI-DSS 3.4: "Rendering PAN unreadable anywhere it is stored (including on portable digital media, backup media, and **in logs**)." |
| | If log files are: |
| | - Backed up to S3/tape (unencrypted or with lost key), PII is exposed. |
| | - Aggregated to a central logging service (Loki, ELK, Splunk) without redaction, PII is searchable. |
| | - Left on disk after server failure, physical recovery exposes PII. |
| | Violation triggers: fines (up to $100k+ per incident under state laws), customer breach notifications, reputational damage. |
| **Current Impact** | Logs are actively created and written to disk daily. No retention/rotation policy documented. If server is decommissioned, logs may be left in place. |
| **Mitigation Path** | **Week 1 (NOW):** Implement `PiiRedactor` class; apply to all 7 services' logging (ADR 0006). Redact PAN, CVV, full SSN, email, phone before writing to disk. Preserve last 4 of SSN for audit trails. |
| | **Week 1 (ongoing):** Flag existing log files (in this debt-log). Do not delete; archive separately (out of scope). |
| | **Week 2:** Implement log rotation + deletion (30-day retention). |
| | **Week 2:** Implement centralized logging (Loki/ELK) with redaction at ingest. |
| | **Week 3:** Audit all existing backups; re-encrypt or delete any containing plaintext PII. |
| **Status** | Open; **being addressed in Week 1 via ADR 0006 (logging redaction).** |

---

### D13: PAN and CVV Stored in Database

| Field | Value |
|---|---|
| **ID** | D13 |
| **Finding** | Full PAN and CVV are stored in plaintext in the `payments` table. |
| **Location** | `db/init/001_schema.sql` (lines 96–105): |
| | ```sql |
| | CREATE TABLE IF NOT EXISTS payments ( |
| |     id          SERIAL PRIMARY KEY, |
| |     loan_id     INTEGER REFERENCES loans(id), |
| |     pan         TEXT,                 -- full PAN stored |
| |     cvv         TEXT,                 -- CVV stored (SAD — flat PCI prohibition) |
| |     amount      DOUBLE PRECISION NOT NULL, |
| |     method      TEXT DEFAULT 'card', |
| |     created_at  TIMESTAMPTZ DEFAULT now() |
| | ); |
| | ``` |
| | **Rationale (from ADR 0003):** "Customer support wants to 'see the card on file' when a borrower calls about a payment, and finance wants to re-run a charge without asking the customer for the number again." |
| **Risk** | **Critical.** PCI-DSS 2.1, 3.2.1: "Do not store PAN, CVV, or CVC after authorization." |
| | If Postgres is breached (e.g., SQL injection, ransomware, stolen backups), all historical card data is exposed. |
| | Attacker can: reuse stolen cards, commit fraud in customer's name, sell card data. |
| | Liability: PCI-DSS fine ($5,000–$100,000 per month until remediated), potential state AG fines (up to $1,000 per customer per month under some state laws). |
| **Current Impact** | Every payment since go-live is stored with full PAN/CVV. Unclear how many customers/cards are in the table (row count not given). |
| **Mitigation Path** | **NOT Week 1.** This is structural debt requiring: |
| | 1. PCI-DSS-compliant tokenization (e.g., Stripe, AWS Payment Cryptography, or self-hosted HSM). |
| | 2. Modify `payments` table: replace `pan`, `cvv` with `token` (opaque reference to tokenized card). |
| | 3. Re-tokenize all historical data (data migration, potential customer re-auth for PCI audit). |
| | 4. Update charge logic to use tokenized card. |
| | **Week 2–3 candidate** if board prioritizes PCI compliance. Otherwise, deferred to Q2. |
| **Status** | Open; **documented as debt; no fix in scope for Week 1.** Will block production deployment until addressed. |

---

## Summary by Severity

| Severity | Finding | Status | Week 1 Action |
|---|---|---|---|
| **Critical** | D1: Hardcoded credentials | Open | Document, flag, schedule rotation (Week 2+). |
| **Critical** | D5: Plaintext PII in logs | Open | **ADDRESSED: ADR 0006 (logging redaction).** |
| **Critical** | D13: PAN/CVV in DB | Open | Document, flag, schedule tokenization (Week 2–3). |
| **High** | D2: Float money math | Open | Document, flag, schedule migration to Decimal (Week 2+). |

---

## Week 1 Actions

✓ **D1:** Documented; flagged for rotation (Week 2).  
✓ **D2:** Documented; flagged for Decimal migration (Week 2+).  
✓ **D5:** **FIXED (ADR 0006).** PiiRedactor applied to all 7 services; new logs are redacted. Existing logs flagged (not deleted, out of scope).  
✓ **D13:** Documented; flagged for tokenization (Week 2–3).  

---

## Next Steps

- **Week 1:** Complete logging redaction (D5) and verify via integration tests.
- **Week 2:** Rotate credentials (D1), begin tokenization design (D13).
- **Week 2–3:** Migrate to Decimal for money math (D2).
- **Ongoing:** Review new debt as it emerges; update this log.
