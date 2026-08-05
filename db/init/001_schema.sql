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
    monthly_debt      DOUBLE PRECISION,            -- money as float; model DTI input
    employer          TEXT,
    job_title         TEXT,
    employment_years  DOUBLE PRECISION,
    status            TEXT DEFAULT 'submitted',
    -- ADR 0010 Phase B: unguessable per-application continuation token issued at submit.
    -- Authorizes the anonymous applicant to complete decision/offer/accept on THIS
    -- application only (a scoped capability), so anonymous apply keeps working without a
    -- login while serial-id IDOR stays closed. NULL for officer-created/legacy rows.
    -- Stores a KEYED HASH of the token, never the raw token (PR #7 review): the raw token
    -- is returned to the applicant once at submit; a DB read yields only the digest.
    -- Cleared to NULL when the application is funded (single-use at the money action).
    continuation_token TEXT,
    -- Expiry of the continuation token (PR #7 review): authz rejects it past this instant,
    -- so the bearer capability is time-boxed. NULL for officer/legacy rows (no token path).
    continuation_token_expires_at TIMESTAMPTZ,
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
-- One boarded loan per application: makes offer acceptance idempotent under retries and
-- concurrency (a double-click / timeout-retry / concurrent POST cannot board a second
-- loan for the same app; the loser gets a UniqueViolation and replays the first loan).
-- Partial so any legacy app_id-less loan row is unaffected.
CREATE UNIQUE INDEX IF NOT EXISTS uq_loans_app ON loans (app_id) WHERE app_id IS NOT NULL;

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

-- One current offer per application: makes offer generation idempotent so a double-click /
-- timeout-retry / concurrent POST cannot persist duplicate regulated TILA/Reg-Z disclosures
-- (disclosure-service create_offer reuses the existing offer; the concurrent loser gets a
-- UniqueViolation and replays it). Partial so any legacy app_id-less offer row is unaffected.
-- Mirrors uq_loans_app above; disclosure-service /health reports schema_not_ready:uq_offers_app
-- until it exists. Also shipped as migration 0010 for already-initialized volumes (which may
-- need a manual dedup first -- see that file). This unique index also serves the plain
-- app_id lookups (it replaces the former non-unique idx_offers_app).
CREATE UNIQUE INDEX IF NOT EXISTS uq_offers_app ON offers (app_id) WHERE app_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- ADR 0012 / spec D3: disclosure provenance chain (FK-as-graph).
--
-- The knowledge graph is foreign keys in this schema -- node = row, edge = FK. Two edges
-- were missing, so a disclosure could not be traced to the decision and inputs that
-- produced it: offers had app_id only, and there was no disclosure record at all.
--
--   applicants <- applications <- decision_events <- offers <- disclosures
--
-- `decisions` is the mutable current-state pointer and is deliberately NOT part of the
-- chain; `decision_events` (append-only, ADR 0009) is the system of record.
-- ---------------------------------------------------------------------------

-- Edge: which decision produced this offer. NULLABLE on purpose -- offers rows predating
-- ADR 0012 have no provable decision event, and inferring one where an application has
-- several would fabricate provenance. The write path requires it for new offers; legacy
-- rows keep NULL and surface as a partial chain in v_disclosure_provenance.
ALTER TABLE offers ADD COLUMN IF NOT EXISTS decision_event_id INTEGER REFERENCES decision_events(id);
CREATE INDEX IF NOT EXISTS idx_offers_decision_event ON offers(decision_event_id);

-- The authoritative disclosure record. Unlike `offers` (DOUBLE PRECISION, kept as a
-- rounded convenience copy -- debt D2), money here is integer MINOR UNITS and the APR is
-- exact NUMERIC: these are the values with TILA legal weight.
CREATE TABLE IF NOT EXISTS disclosures (
    id                      SERIAL PRIMARY KEY,
    offer_id                INTEGER NOT NULL REFERENCES offers(id),
    decision_event_id       INTEGER NOT NULL REFERENCES decision_events(id),
    status                  TEXT NOT NULL DEFAULT 'draft',
    apr                     NUMERIC(9,3) NOT NULL,
    finance_charge_cents    BIGINT NOT NULL,
    amount_financed_cents   BIGINT NOT NULL,
    monthly_payment_cents   BIGINT NOT NULL,
    total_of_payments_cents BIGINT NOT NULL,
    -- Inputs actually used, so the disclosure can be recomputed without re-deriving them:
    -- {principal_cents, note_rate_pct, term_months, fee_pct}. Identifier-free (ADR 0007).
    compute_snapshot        JSONB NOT NULL,
    fee_schedule_version    TEXT NOT NULL,
    apr_method_version      TEXT NOT NULL,
    -- hash(inputs + ruleset + outputs): detects post-hoc edits to a regulated document.
    content_fingerprint     TEXT NOT NULL,
    -- The borrower-facing document as assembled: {heading, figures, payment_terms,
    -- prepayment}. Identifier-free -- the prose fields are digit-free by output schema and
    -- carry no applicant attributes, so this stores the same class of data the figures
    -- above already do. NOT part of content_fingerprint: the fingerprint covers inputs +
    -- ruleset + outputs, and every figure in here is checked against those outputs before
    -- the row is written, so folding the prose in would make a regulated integrity hash
    -- depend on model wording.
    --
    -- Nullable, because delivery -- not insertion -- is the step that requires it. A row
    -- written without a document is a draft that can never be delivered (the lifecycle
    -- refuses it), which is the fail-closed direction.
    document_body           JSONB,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    delivered_at            TIMESTAMPTZ,
    CONSTRAINT disclosures_status_valid
        CHECK (status IN ('draft', 'in_review', 'approved', 'delivered')),
    -- delivered_at and status cannot disagree in either direction.
    CONSTRAINT disclosures_delivered_at_matches_status
        CHECK ((status = 'delivered') = (delivered_at IS NOT NULL)),
    CONSTRAINT disclosures_amounts_nonnegative
        CHECK (finance_charge_cents >= 0 AND amount_financed_cents > 0
               AND monthly_payment_cents > 0 AND total_of_payments_cents > 0),
    CONSTRAINT disclosures_apr_nonnegative CHECK (apr >= 0)
);

-- One disclosure per offer: same idempotency guarantee uq_offers_app gives the offer
-- itself, for the same reason -- a retry or concurrent POST must not persist duplicate
-- regulated TILA records. Re-issue after delivery (which would supersede rather than
-- replace) arrives with a real delivery channel; there is no column for it yet because an
-- unused nullable pointer with no defined semantics invites writes that mean nothing.
CREATE UNIQUE INDEX IF NOT EXISTS uq_disclosures_offer ON disclosures (offer_id);
CREATE INDEX IF NOT EXISTS idx_disclosures_decision_event ON disclosures(decision_event_id);
CREATE INDEX IF NOT EXISTS idx_disclosures_status ON disclosures(status);

-- Delivered disclosures are frozen. NOT unconditional append-only (contrast
-- decision_events): draft -> in_review -> approved are legitimate mutations of a document
-- that has not reached the borrower. Once it HAS, the row is the record of what they were
-- shown, and correcting it in place would destroy that evidence.
CREATE OR REPLACE FUNCTION disclosures_freeze_delivered() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'delivered' THEN
        RAISE EXCEPTION
            'disclosure % is delivered and immutable (ADR 0012): % blocked', OLD.id, TG_OP;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_disclosures_freeze_delivered ON disclosures;
CREATE TRIGGER trg_disclosures_freeze_delivered
    BEFORE UPDATE OR DELETE ON disclosures
    FOR EACH ROW EXECUTE FUNCTION disclosures_freeze_delivered();

-- Row-level triggers do not fire on TRUNCATE, which would erase delivered rows wholesale.
CREATE OR REPLACE FUNCTION disclosures_no_truncate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'disclosures cannot be truncated (ADR 0012): delivered rows are immutable';
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_disclosures_no_truncate ON disclosures;
CREATE TRIGGER trg_disclosures_no_truncate
    BEFORE TRUNCATE ON disclosures
    FOR EACH STATEMENT EXECUTE FUNCTION disclosures_no_truncate();

-- The graph read: one query walks disclosure -> offer -> decision_event -> application ->
-- applicant. Anchored on `offers` with LEFT JOINs so a legacy offer carrying neither a
-- decision_event_id nor a disclosure still appears as a PARTIAL chain -- a provenance view
-- that silently omitted the rows with the worst provenance would invert its own purpose.
-- Identifiers only: no name, SSN, DOB or address crosses into this view (ADR 0007).
-- Dropped rather than CREATE OR REPLACE'd: replace cannot insert a column into the middle
-- of a view's column list, so any future shape change would fail on re-apply. A view holds
-- no data, so dropping it costs nothing and keeps this file re-runnable.
DROP VIEW IF EXISTS v_disclosure_provenance;
CREATE VIEW v_disclosure_provenance AS
SELECT
    o.id                    AS offer_id,
    o.created_at            AS offer_created_at,
    -- The offer's own float APR, so a legacy row with no disclosures record still shows a
    -- number. Where both are present `disclosed_apr` is authoritative and this is the
    -- rounded convenience copy (debt D2); where they disagree, trust disclosures.
    o.apr                   AS offer_apr,
    d.id                    AS disclosure_id,
    d.status                AS disclosure_status,
    d.apr                   AS disclosed_apr,
    -- The exact inputs the figures were derived from (principal, rate, term, fee_pct).
    -- Carried here so acceptance D3.3 holds literally: ONE query on this view answers
    -- "what was disclosed, from what inputs, under which ruleset" without a second read
    -- of `disclosures`. Identifier-free by construction (ADR 0007).
    d.compute_snapshot,
    d.fee_schedule_version,
    d.apr_method_version,
    d.content_fingerprint,
    d.delivered_at,
    de.id                   AS decision_event_id,
    de.outcome              AS decision_outcome,
    de.policy_band,
    de.decided_at,
    app.id                  AS application_id,
    app.applicant_id        AS applicant_id
FROM offers o
LEFT JOIN disclosures d      ON d.offer_id = o.id
LEFT JOIN decision_events de ON de.id = o.decision_event_id
LEFT JOIN applications app   ON app.id = o.app_id;
