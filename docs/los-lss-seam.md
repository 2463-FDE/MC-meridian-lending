# LOS↔LSS Seam Map

**Date:** 2026-07-01  
**Scope:** How funded loans flow from Loan Origination System (LOS) to Loan Servicing System (LSS)

---

## The Seam: Direct Cross-Schema Insert

```
LOS (origination-service)                    LSS (servicing-service)
─────────────────────────────────────────────────────────────────

Borrower applies
    ↓
Application intake
    ↓
KYC / Decision / Disclosure
    ↓
Loan funded / decision = 'approve'
    ↓
board_to_servicing() called
    │
    └─→ [DIRECT SQL INSERT]
         No API call
         No event
         No async message
         ┌─────────────────────────────────────┐
         │ INSERT INTO loans (...)             │
         │ INSERT INTO balances (...)          │
         └─────────────────────────────────────┘
            ↓
         LSS owns the loan
         Loan appears in servicing system
         Balance tracking begins
```

---

## Code References

### Origination Side

**File:** `services/origination-service/app/intake.py`

**Function:** `board_to_servicing(app_id, applicant_name, principal, annual_rate_pct, term_months)`

**Lines 36–52:**
```python
def board_to_servicing(app_id: int, applicant_name: str, principal: float,
                       annual_rate_pct: float, term_months: int) -> int:
    """Direct cross-schema insert into the LSS tables. The 'seam'."""
    loan = db.query(
        "INSERT INTO loans (app_id, applicant_name, principal, apr, term_months) "
        "VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (app_id, applicant_name, principal, annual_rate_pct, term_months),
    )
    loan_id = loan[0]["id"]
    # reach across into the servicing balances table directly
    db.query(
        "INSERT INTO balances (loan_id, balance) VALUES (%s, %s) "
        "ON CONFLICT (loan_id) DO NOTHING",
        (loan_id, float(principal)),   # money as float
    )
    log.info("boarded app_id=%s -> loan_id=%s (direct LSS insert)", app_id, loan_id)
    return loan_id
```

**Called from:** `services/origination-service/app/routers/applications.py` (after decision + offer are finalized, typically when a borrower accepts an offer).

---

## Schema Mapping

### Origination Side (Input)

| Table | Columns | Source |
|---|---|---|
| `applicants` | `id, name, dob, ssn, ein, is_entity, email, phone, address, created_at` | Borrower intake form |
| `applications` | `id, applicant_id, amount, term_months, purpose, income, employer, job_title, employment_years, status, created_at` | Borrower intake + auto-generated fields |
| `decisions` | `app_id, outcome` | decision-service (KYC + credit pull) |
| `offers` | `id, app_id, apr, finance_charge, monthly_payment, amount_financed, total_of_payments, created_at` | disclosure-service (TILA/Reg-Z calculation) |

### Servicing Side (Output)

| Table | Columns | Populated By | Source |
|---|---|---|---|
| `loans` | `id, app_id, applicant_name, principal, apr, term_months, status, opened_at` | `board_to_servicing()` | origination LOS insert |
| `balances` | `loan_id, balance, past_due, updated_at` | `board_to_servicing()` | LOS insert (initial balance = principal) |

---

## Data Fields Crossing the Seam

### Explicit: Passed as Parameters to board_to_servicing()

| Field | LOS Source | LSS Table | LSS Column | Notes |
|---|---|---|---|---|
| `app_id` | `applications.id` | `loans` | `app_id` | FK back to origination application |
| `applicant_name` | `applicants.name` | `loans` | `applicant_name` | Denormalized; text copy, not FK |
| `principal` | `applications.amount` or `offers.amount_financed` | `loans` | `principal` | Money as DOUBLE PRECISION (float) |
| `annual_rate_pct` | `offers.apr` | `loans` | `apr` | APR as float (rounding risk) |
| `term_months` | `applications.term_months` | `loans` | `term_months` | Loan duration in months |

### Implicit: Defaults / System-Generated

| Field | LSS Table | LSS Column | Value | Notes |
|---|---|---|---|---|
| `loan_id` | `loans` | `id` | SERIAL, auto-gen | Primary key for the loan in LSS |
| `balance` | `balances` | `balance` | `principal` | Initialized to loan amount; decremented by payments |
| `status` | `loans` | `status` | `'current'` | Hard-coded default; not set by LOS |
| `opened_at` | `loans` | `opened_at` | `NOW()` | Server-side timestamp at boarding time |

---

## Known Gaps & Debt

### 1. No Event / No Notification

**Gap:** When a loan is boarded, there is no:
- Event published (e.g., `LoanBoarded` event to a message queue)
- API call from LOS to LSS (e.g., `POST /loans`)
- Notification to LSS that a new loan exists

**Impact:**
- LSS learns about the loan only because the insert happened in the shared DB.
- No audit trail of the boarding action (it's a raw SQL insert, not a named operation).
- If LSS were split to a separate database, this would break immediately.

**Mitigation Path (future):**
- Add an event table (e.g., `boarding_events`) or use a message queue (e.g., Redis/RabbitMQ/Kafka).
- Publish `LoanBoarded(app_id, loan_id, principal, apr, term_months)` after insert succeeds.
- Decouple LOS from LSS for true microservice independence.

### 2. No Saga / No Rollback on Failure

**Gap:** If the insert into `balances` fails (e.g., FK constraint, disk full), the `loans` insert has already committed. Origination believes the loan is boarded, but LSS is in an inconsistent state (loan exists, no balance).

**Impact:** Loan servicing fails or is incomplete for that loan.

**Mitigation Path (future):**
- Wrap both inserts in a transaction.
- Or: use idempotent re-boarding with a unique constraint (e.g., `loans(app_id)` unique).

### 3. Data Denormalization Risk

**Gap:** `loans.applicant_name` is copied from `applicants.name`. If the applicant's name is updated later in the origination schema, the servicing schema is not updated.

**Impact:** Loan record has stale applicant name.

**Mitigation Path (future):**
- Store only `app_id` and `applicant_id` in `loans`; look up name via FK when needed.
- Or: accept denormalization and document it (current state).

### 4. Float Money Math

**Gap:** `principal` and all money fields are stored and computed as `DOUBLE PRECISION` (float). Rounding errors compound.

**Example:** 
- Loan amount: $10,000 / 36 months = $277.777... per month
- Stored as float: loses precision.
- After 36 months: balance may not be exactly $0 due to rounding.

**Impact:** Payment reconciliation fails; audit trail shows discrepancies.

**Mitigation Path (future):**
- Migrate to `NUMERIC(19,2)` (fixed-point, cents).
- All calculations in cents, not dollars.

---

## Summary

**Boarding is a **direct, synchronous, in-database seam**:**
- Origination (LOS) inserts directly into servicing (LSS) tables.
- No event, no API, no notification.
- No rollback protection if LSS insert fails.
- Data is denormalized and uses float (rounding risk).

**This design is:**
- ✓ Simple (works in a single shared Postgres DB).
- ✗ Fragile (LSS is not independent; hard to split databases later).
- ✗ Risky (no saga, no rollback, direct DB coupling).

**Improvement roadmap:**
1. Add event / notification mechanism (week 3+).
2. Wrap in transaction and test failure paths (week 2+).
3. Document FK constraints and uniqueness invariants (week 2).
4. Plan for database split and async messaging (quarter 2+).
