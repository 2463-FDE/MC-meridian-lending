-- 0012_disclosures.sql — ADR 0012 / spec D3: disclosure provenance chain (FK-as-graph).
--
-- Compose mounts db/init/* only, so this file exists for volumes that were already
-- initialized before ADR 0012. The DDL below is IDENTICAL to what db/init/001_schema.sql
-- now contains; commit e0716da is the bug that happens when the two drift (an index that
-- lived only in a migration left a replay path dead and /health permanently
-- schema_not_ready). Change both or neither.
--
-- SAFE ON A POPULATED VOLUME. Every statement is additive:
--   * offers.decision_event_id is added NULLABLE. `ADD COLUMN ... NOT NULL` would fail
--     outright against existing offers rows, and there is no honest default -- an offer
--     written before this migration has no provable originating decision event.
--   * disclosures is a new table, so its NOT NULL columns constrain nothing retroactively.
--   * the view LEFT JOINs from offers, so pre-existing offers appear as a partial chain
--     rather than vanishing.
--
-- OPTIONAL BACKFILL, MANUAL AND OPERATOR-RUN -- deliberately not automated here, for the
-- same reason migration 0010 leaves its dedup manual: this writes provenance onto a
-- regulated record, and a script that guesses is worse than a NULL that admits it does
-- not know. Populate ONLY where an application has exactly one decision event:
--
--     -- inspect first: which offers can be linked unambiguously?
--     SELECT o.id AS offer_id, o.app_id, count(de.id) AS candidate_events
--     FROM offers o LEFT JOIN decision_events de ON de.app_id = o.app_id
--     WHERE o.decision_event_id IS NULL
--     GROUP BY o.id, o.app_id ORDER BY candidate_events DESC;
--
--     -- then link only the unambiguous ones; leave the rest NULL for review:
--     UPDATE offers o SET decision_event_id = de.id
--     FROM decision_events de
--     WHERE de.app_id = o.app_id
--       AND o.decision_event_id IS NULL
--       AND (SELECT count(*) FROM decision_events x WHERE x.app_id = o.app_id) = 1;
--
-- Existing offers keep their existing money values. They are NOT recomputed under the
-- corrected actuarial method: the figure disclosed to the borrower is the legally
-- operative one, and overwriting it would destroy the evidence of what was disclosed.
-- Absence of a disclosures row means "computed under the pre-ADR-0012 add-on method."
-- Whether the back book is quantified and cured is a client decision, not a migration.
--
-- disclosure-service /health reports schema_not_ready:<object> until each object exists,
-- so a code-ahead-of-migration deploy fails loudly instead of writing half-linked
-- provenance.

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
    de.policy_band,
    de.decided_at,
    app.id                  AS application_id,
    app.applicant_id        AS applicant_id
FROM offers o
LEFT JOIN disclosures d      ON d.offer_id = o.id
LEFT JOIN decision_events de ON de.id = o.decision_event_id
LEFT JOIN applications app   ON app.id = o.app_id;
