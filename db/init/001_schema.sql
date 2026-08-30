-- Meridian Lending — schema (Halcyon v1, extended in-place over the years)
-- NOTE: money is stored as double precision throughout. Keeps the app code simple.

-- Staff + borrower logins. Roles: admin | underwriter | csr | borrower.
CREATE TABLE IF NOT EXISTS users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    -- D27: salted PBKDF2-HMAC-SHA256 ("pbkdf2_sha256$<iterations>$<salt>$<hash>",
    -- services/gateway/app/auth.py). A row may still hold the pre-fix bare sha256(password)
    -- hex -- the seed does, on purpose, so a fresh volume exercises the migration path --
    -- authenticate() rehashes it to the new format on that row's next successful login.
    password_hash TEXT NOT NULL,
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
    created_at  TIMESTAMPTZ DEFAULT now(),
    -- Postgres DATE reaches year 294276; Python's date stops at 9999 and has no BC. A dob
    -- outside that window stores fine and then raises "year N is out of range" when the
    -- ORM hydrates the Applicant row -- which the Application.applicant relationship does
    -- eagerly (lazy="joined"), so ONE such row breaks the whole officer queue, not just
    -- its own application. Constrain storage to what the readers can represent. The 1900
    -- floor stays in the request validator (schemas.py::_validate_dob): implausibly old is
    -- an input-policy question, unreadable is an availability one. Mirrors migration 0011;
    -- origination-service /health reports schema_not_ready:ck_applicants_dob_readable
    -- until it exists.
    CONSTRAINT ck_applicants_dob_readable
        CHECK (dob IS NULL OR (dob >= DATE '0001-01-01' AND dob <= DATE '9999-12-31'))
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
    -- D24 (PR #38 review): users.id of the caller who submitted this application, when they
    -- were authenticated (X-User-Id) at submit time. NULL for a genuinely anonymous apply --
    -- that is the common case and is not itself a signal. Lets deny_self_decision refuse an
    -- officer who submitted their OWN application through the ordinary apply flow, which
    -- users.applicant_id == applications.applicant_id cannot see (intake never links the two).
    submitted_by_user_id INTEGER,
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
    apr             DOUBLE PRECISION NOT NULL,   -- disclosed actuarial APR (display); carries the fee
    note_rate       DOUBLE PRECISION,            -- contractual rate servicing amortizes at; NULL=legacy loan, fall back to apr
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

-- Payments: stores the full PAN (D13b, still open). The CVV column was deleted by
-- migration 0020 (D13a / ADR 0013 Decision 2) — retaining sensitive authentication data
-- after authorization is a flat PCI-DSS 3.2.1 prohibition, so the remediation was a
-- deletion of the values and the column, not merely ceasing to write it. Do not
-- reintroduce it. Carries an idempotency key as of migration 0018 (D19 / ADR 0013
-- Decision 1) — a retried POST no longer charges twice.
CREATE TABLE IF NOT EXISTS payments (
    id          SERIAL PRIMARY KEY,
    loan_id     INTEGER REFERENCES loans(id),
    pan         TEXT,                 -- full PAN stored (D13b)
    amount      DOUBLE PRECISION NOT NULL,  -- money as float (D2, left alone here)
    method      TEXT DEFAULT 'card',
    created_at  TIMESTAMPTZ DEFAULT now(),
    -- D19 (migration 0018). Kept in step with db/migrations/0018_payments_idempotency.sql;
    -- test_payments_idempotency_ddl_parity compares the two declarations.
    idempotency_key           TEXT,        -- client-minted, retired to NULL past its window
    idempotency_expires_at    TIMESTAMPTZ, -- stamped at insert; the window is a column, not
                                           -- an index predicate (now() is not immutable)
    request_fingerprint       TEXT,        -- distinguishes a genuine replay from a reused
                                           -- key carrying a different payload (422)
    status      TEXT NOT NULL DEFAULT 'captured',  -- 'captured' is what every pre-0018 row
                                           -- factually is; only a terminal row releases its key
    processor_idempotency_key TEXT,        -- per-ROW, deliberately NOT the client's key: the
                                           -- two retention windows must not couple
    processor_ref             TEXT,
    amount_minor              BIGINT,      -- integer minor units for what this design adds
                                           -- (ADR 0012 precedent); `amount` above stays float
    updated_at                TIMESTAMPTZ
);

-- D3 (migration 0019 / ADR 0020). The append-only record of which payment moved which
-- balance. `balances.balance` is a single mutable column with no history, so before this
-- table there was no fact anywhere saying a given payment had been applied — the apply was
-- inferred from an HTTP status. UNIQUE (payment_id) is what makes a replay a no-op instead
-- of a second credit, and the row commits in the SAME transaction as the balance UPDATE.
-- Kept byte-identical to db/migrations/0019_payment_applications.sql;
-- test_payment_applications_ddl_parity compares the two declarations.
CREATE TABLE IF NOT EXISTS payment_applications (
    id           SERIAL PRIMARY KEY,
    loan_id      INTEGER NOT NULL REFERENCES loans(id),
    payment_id   INTEGER NOT NULL UNIQUE REFERENCES payments(id),
    amount_minor BIGINT NOT NULL,        -- integer minor units, the amount of record
                                         -- (ADR 0012); balances.balance stays float (D2)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "audit" log (D20): append-only, enforced by trigger below. deleted_at predates the
-- trigger and is now inert -- any UPDATE it would need is blocked the same as every
-- other UPDATE -- kept rather than dropped because nothing in this change touches column
-- shape, only mutability.
CREATE TABLE IF NOT EXISTS audit_logs (
    id          SERIAL PRIMARY KEY,
    actor       TEXT,
    action      TEXT,
    detail      TEXT,
    deleted_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ DEFAULT now()
);

-- Append-only enforced at the database (D20), mirroring decision_events below: the
-- audit trail must survive application bugs and ad-hoc SQL, not just code convention.
CREATE OR REPLACE FUNCTION audit_logs_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_logs is append-only (D20): % blocked', TG_OP;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_audit_logs_append_only ON audit_logs;
CREATE TRIGGER trg_audit_logs_append_only
    BEFORE UPDATE OR DELETE ON audit_logs
    FOR EACH ROW EXECUTE FUNCTION audit_logs_append_only();

-- Row-level triggers do not fire on TRUNCATE; block it explicitly.
DROP TRIGGER IF EXISTS trg_audit_logs_no_truncate ON audit_logs;
CREATE TRIGGER trg_audit_logs_no_truncate
    BEFORE TRUNCATE ON audit_logs
    FOR EACH STATEMENT EXECUTE FUNCTION audit_logs_append_only();

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

-- Append-only enforced at the database (audit_logs above got the same guarantee, D20).
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

-- A few indexes added over time for the servicing dashboard. (No reason/driver columns
-- on decisions.)
CREATE INDEX IF NOT EXISTS idx_loans_status ON loans(status);
CREATE INDEX IF NOT EXISTS idx_payments_loan ON payments(loan_id);

-- D19 / ADR 0013 Decision 1. The double-charge guarantee lives HERE, not in a service:
-- two handlers write payments (payment-service and servicing-service, debt D23) and a
-- support engineer with psql is a third, so a check in application code protects one
-- writer and not the others.
--
-- Both indexes are PARTIAL. That is load-bearing twice over: pre-migration rows carry a
-- NULL key and must stay valid, and retiring an expired key (setting it back to NULL on a
-- terminal row) drops that row out of the index so the value can be claimed again.
--
-- Every insert against these must spell the predicate in its conflict target —
-- `ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL` — or Postgres cannot
-- infer the arbiter and raises at runtime, disabling the control on first use. Vector
-- R-DDL runs the shipped SQL string against a real Postgres for exactly this reason.
CREATE UNIQUE INDEX IF NOT EXISTS payments_idempotency_key_uniq
  ON payments (idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS payments_processor_idempotency_key_uniq
  ON payments (processor_idempotency_key) WHERE processor_idempotency_key IS NOT NULL;

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
    -- The disclosure record's OWN decision edge, alongside the offer-derived one above.
    -- `decision_event_id` walks offers.decision_event_id (the offer's edge); this is
    -- disclosures.decision_event_id (the edge stamped on the regulated record at write).
    -- The write path keeps the two equal (create refuses a mismatch, replay closes the
    -- offer edge from this column), but a row predating that guard -- or an operator
    -- backfill of offers.decision_event_id -- can diverge, and a completeness check that
    -- only asks "is each edge non-null" reports chain_complete over that split-brain. Both
    -- are exposed so the reader compares them; delivery refuses when they disagree.
    d.decision_event_id     AS disclosure_decision_event_id,
    de.outcome              AS decision_outcome,
    -- The outcome of the decision the REGULATED disclosure record ITSELF cites, alongside
    -- `decision_outcome` (which walks the OFFER's edge). A disclosure is a regulated artifact
    -- for an APPROVED decision only; the create guard now refuses a non-approve edge, but a
    -- back-book row written before that guard -- or an operator backfill of
    -- disclosures.decision_event_id -- can name a deny/refer decision while every edge is
    -- non-null, and a completeness check that only asks "is each edge non-null" reports
    -- chain_complete over it. Exposed so the reader compares outcomes rather than infers;
    -- delivery refuses when this is present and not 'approve'. NULL for the offer-edge copy
    -- (`decision_outcome`) on a legacy no-edge offer, which is exactly why the disclosure's
    -- OWN edge is walked here rather than reusing that column.
    dde.outcome             AS disclosure_decision_outcome,
    de.policy_band,
    de.decided_at,
    app.id                  AS application_id,
    app.applicant_id        AS applicant_id
FROM offers o
LEFT JOIN disclosures d       ON d.offer_id = o.id
LEFT JOIN decision_events de  ON de.id = o.decision_event_id
LEFT JOIN decision_events dde ON dde.id = d.decision_event_id
LEFT JOIN applications app    ON app.id = o.app_id;

-- >>> assistant_runs DDL (init/migration parity block -- byte-identical in both files)
-- Assistant run telemetry. One row per officer assistant request that reached the entry
-- span (services/origination-service/app/main.py), refused or served.
--
-- WHY A TABLE AND NOT THE TRACE. The spans are content-free by design, and `trace()` is a
-- no-op unless LANGSMITH_TRACING is set. LangSmith can therefore answer "what did this one
-- run do" but never "what fraction of runs refused last week": its population is whatever
-- happened to be exported. This row is written either way, which is what makes an
-- aggregate over it honest.
--
-- application_id CARRIES NO FOREIGN KEY, deliberately. `not_found` refusals are exactly
-- the rows whose id references nothing, so a FK could only reject those rows or null the
-- column -- and a run of requests against ids that do not exist is itself the signal
-- (a broken officer link, or id enumeration). Same shape as
-- applications.submitted_by_user_id. This is telemetry, not a regulated artifact: ADR
-- 0012's FK-as-provenance governs the decision -> offer -> disclosure chain, and there is
-- no append-only trigger here for that same reason (contrast decision_events).
--
-- NO FREE TEXT, and refusal_code is CHECK-constrained rather than bare TEXT on purpose.
-- The entry span's own comment records why: httpx.HTTPStatusError's message embeds the
-- request URL (which embeds app_id) and an LLMError can carry raw provider text, so an
-- unconstrained column invites `str(exc)` and reintroduces precisely what that span
-- strips before export.
CREATE TABLE IF NOT EXISTS assistant_runs (
    id                  BIGSERIAL PRIMARY KEY,
    -- LangSmith's own run id. Opaque outside this database, so it carries none of the
    -- exposure the omitted application_id/request_id do on the spans themselves.
    trace_id            TEXT NOT NULL,
    application_id      INTEGER NOT NULL,
    task                TEXT NOT NULL,
    policy_topic        TEXT,
    http_status         INTEGER NOT NULL,
    refusal_code        TEXT,
    -- Served-run columns. NULL on a refusal, and NULL individually when _charted() left
    -- the key off because the result did not carry it.
    outcome             TEXT,
    record_status       TEXT,
    policy_band         TEXT,
    narration_validated BOOLEAN,
    policy_citations    INTEGER,
    policy_searches     INTEGER,
    latency_ms          INTEGER NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_assistant_runs_task
        CHECK (task IN ('decision', 'explain')),
    -- Every code _run_assistant can record. never_decisioned is the half of the old
    -- single `not_found` that means the application EXISTS but carries no decision
    -- record; fusing the two made a broken-link spike and an asked-too-early spike one
    -- number with opposite remedies.
    CONSTRAINT ck_assistant_runs_refusal_code
        CHECK (refusal_code IS NULL OR refusal_code IN (
            'not_found', 'never_decisioned', 'assistant_refused', 'llm_unavailable',
            'kyc_blocked', 'refused', 'idempotency_conflict', 'downstream_unavailable'
        )),
    -- A row is either a served answer or a refusal, never ambiguously both -- so a
    -- refusal can never be read as "outcome unknown". Keyed on http_status and NOT on
    -- `outcome`: _charted() omits outcome when the result does not carry it, so an
    -- outcome-keyed constraint would reject a legitimately served row and lose it. The
    -- control flow guarantees this form -- refusal is None if and only if the response
    -- is 200.
    CONSTRAINT ck_assistant_runs_refusal_matches_status
        CHECK ((http_status = 200) = (refusal_code IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_assistant_runs_created ON assistant_runs(created_at);
CREATE INDEX IF NOT EXISTS idx_assistant_runs_app ON assistant_runs(application_id);
-- <<< assistant_runs DDL
