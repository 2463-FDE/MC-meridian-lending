-- Meridian Lending — schema (Halcyon v1, extended in-place over the years)
-- NOTE: money is stored as double precision throughout. Keeps the app code simple.

-- Staff + borrower logins. Passwords are sha256 hex (no salt, no bcrypt — Halcyon's
-- "we'll harden it later"). Roles: admin | underwriter | csr | borrower.
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,        -- sha256(password), unsalted
    role          TEXT NOT NULL DEFAULT 'csr',
    display_name  TEXT,
    applicant_id  INTEGER,              -- set for borrower logins
    is_active     BOOLEAN DEFAULT TRUE,
    created_at    TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS applicants (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    dob         DATE,
    ssn         TEXT,            -- plaintext
    ein         TEXT,            -- for entity applicants
    is_entity   BOOLEAN DEFAULT FALSE,
    email       TEXT,
    phone       TEXT,
    address     TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS applications (
    id                SERIAL PRIMARY KEY,
    applicant_id      INTEGER REFERENCES applicants(id),
    amount            DOUBLE PRECISION NOT NULL,   -- money as float
    term_months       INTEGER NOT NULL,
    purpose           TEXT,
    income            DOUBLE PRECISION,            -- money as float
    employer          TEXT,
    job_title         TEXT,
    employment_years  DOUBLE PRECISION,
    status            TEXT DEFAULT 'submitted',
    created_at        TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_applications_applicant ON applications(applicant_id);

-- KYC: CIP only. No sanctions/OFAC, no beneficial owner, no monitoring.
CREATE TABLE IF NOT EXISTS kyc_checks (
    id              SERIAL PRIMARY KEY,
    applicant_id    INTEGER REFERENCES applicants(id),
    name_verified   BOOLEAN,
    dob_verified    BOOLEAN,
    address_verified BOOLEAN,
    ssn_verified    BOOLEAN,
    -- no sanctions_screened, no ubo_identified, no ongoing_monitoring columns
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Decision: OUTCOME ONLY. No reason, no model drivers, no inputs, no timestamp of model run.
CREATE TABLE IF NOT EXISTS decisions (
    app_id      INTEGER PRIMARY KEY REFERENCES applications(id),
    outcome     TEXT NOT NULL   -- 'approve' | 'deny' | 'refer' | 'counteroffer'
);

CREATE TABLE IF NOT EXISTS offers (
    id          SERIAL PRIMARY KEY,
    app_id      INTEGER REFERENCES applications(id),
    apr         DOUBLE PRECISION,    -- float APR (rounding risk)
    finance_charge DOUBLE PRECISION, -- float
    monthly_payment DOUBLE PRECISION,
    amount_financed DOUBLE PRECISION,
    total_of_payments DOUBLE PRECISION,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- LSS tables. A funded loan is "boarded" here by a direct insert from origination.
CREATE TABLE IF NOT EXISTS loans (
    id              SERIAL PRIMARY KEY,
    app_id          INTEGER,
    applicant_name  TEXT,
    principal       DOUBLE PRECISION NOT NULL,   -- money as float
    apr             DOUBLE PRECISION NOT NULL,
    term_months     INTEGER NOT NULL,
    status          TEXT DEFAULT 'current',
    opened_at       TIMESTAMPTZ DEFAULT now()
);

-- Mutable balance: one column, overwritten in place. No ledger, no transaction history.
CREATE TABLE IF NOT EXISTS balances (
    loan_id     INTEGER PRIMARY KEY REFERENCES loans(id),
    balance     DOUBLE PRECISION NOT NULL,   -- money as float, UPDATE-d in place
    past_due    DOUBLE PRECISION DEFAULT 0,
    updated_at  TIMESTAMPTZ DEFAULT now()
);

-- Payments: stores full PAN + CVV. No idempotency key. No unique charge reference.
CREATE TABLE IF NOT EXISTS payments (
    id          SERIAL PRIMARY KEY,
    loan_id     INTEGER REFERENCES loans(id),
    pan         TEXT,                 -- full PAN stored
    cvv         TEXT,                 -- CVV stored (SAD — flat PCI prohibition)
    amount      DOUBLE PRECISION NOT NULL,  -- money as float
    method      TEXT DEFAULT 'card',
    created_at  TIMESTAMPTZ DEFAULT now()
    -- no idempotency_key, no unique(charge_ref)
);

-- "audit" log: an ordinary, mutable table. Rows can be UPDATE/DELETE-d. Not append-only.
CREATE TABLE IF NOT EXISTS audit_logs (
    id          SERIAL PRIMARY KEY,
    actor       TEXT,
    action      TEXT,
    detail      TEXT,
    deleted_at  TIMESTAMPTZ,        -- soft-delete column on an "audit" trail
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- ADR 0009 / ADR 0008: append-only decision-event record. `decisions` above remains the
-- mutable current-state pointer; this is the system of record for Reg B adverse action.
CREATE TABLE IF NOT EXISTS decision_events (
    id                SERIAL PRIMARY KEY,
    app_id            INTEGER NOT NULL REFERENCES applications(id),
    outcome           TEXT NOT NULL,               -- approve | refer | deny | counteroffer
    principal_reasons JSONB NOT NULL,              -- [] for approve; [{code, reason}, ...] for deny/refer
    drivers           JSONB NOT NULL,              -- model score, ranked attributions, band cutoff, model id+version
    policy_band       TEXT NOT NULL,               -- band the score actually landed in
    inputs            JSONB NOT NULL,              -- identifier-free (ADR 0007 rule 1): no SSN/name/DOB/address/PAN
    decided_by        TEXT NOT NULL,               -- model id+version, or user id for manual/override decisions
    decided_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id        TEXT                         -- optional idempotency key; retries replay within the same app_id, absence = explicit re-decision
);
CREATE INDEX IF NOT EXISTS idx_decision_events_app ON decision_events(app_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_decision_events_request
    ON decision_events (app_id, request_id) WHERE request_id IS NOT NULL;

-- Append-only enforced at the database (contrast audit_logs above, which is mutable).
CREATE OR REPLACE FUNCTION decision_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'decision_events is append-only (ADR 0009): % blocked', TG_OP;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_decision_events_append_only ON decision_events;
CREATE TRIGGER trg_decision_events_append_only
    BEFORE UPDATE OR DELETE ON decision_events
    FOR EACH ROW EXECUTE FUNCTION decision_events_append_only();

-- Row-level triggers do not fire on TRUNCATE; block it explicitly.
DROP TRIGGER IF EXISTS trg_decision_events_no_truncate ON decision_events;
CREATE TRIGGER trg_decision_events_no_truncate
    BEFORE TRUNCATE ON decision_events
    FOR EACH STATEMENT EXECUTE FUNCTION decision_events_append_only();

-- A few indexes added over time for the servicing dashboard. (No idempotency index on
-- payments — there is no idempotency key to index. No reason/driver columns on decisions.)
CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status);
CREATE INDEX IF NOT EXISTS idx_payments_loan ON payments(loan_id);
CREATE INDEX IF NOT EXISTS idx_offers_app ON offers(app_id);
