-- 0015_provenance_disclosure_edge.sql — expose the disclosure's own decision edge in
-- v_disclosure_provenance so a split-brain audit trail is detectable.
--
-- Compose mounts db/init/* only, so this file redefines the view for volumes already
-- initialized before this change. db/init/001_schema.sql and migration 0012 now carry the
-- SAME view body (they are byte-identical past the ADR 0012 marker — a test asserts it);
-- this migration reissues that body for a volume where 0012 already ran the older view.
--
-- WHY: the view walked decision_events through offers.decision_event_id only and exposed
-- that as `decision_event_id`. The regulated disclosures row carries its OWN
-- decision_event_id (stamped at write, validated against the offer's application). The two
-- are equal for any row the current write path produced — create refuses a mismatch and
-- replay closes the offer edge from the disclosure's column — but a row written before that
-- guard, or an operator backfill of offers.decision_event_id, can diverge. The completeness
-- check asked only "is each edge non-null", so it reported chain_complete: true over a
-- disclosure whose audit trail named a DIFFERENT decision than the one on the regulated
-- record, and delivery/boarding proceeded. Exposing disclosure_decision_event_id lets the
-- reader compare the two; disclosure-service treats a disagreement as an incomplete chain
-- and refuses delivery.
--
-- SAFE ON A POPULATED VOLUME. A view holds no data; DROP + CREATE only changes the
-- projection. The added column is a plain projection of an existing NOT NULL column, no
-- table rewrite.
--
-- disclosure-service /health probes v_disclosure_provenance for this column, so a deploy
-- whose code reads disclosure_decision_event_id before this migration runs reports
-- schema_not_ready:v_disclosure_provenance instead of 500-ing the provenance route.

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

-- OPERATOR REPORT — find rows whose two decision edges already disagree (the back book the
-- write-path guard cannot reach). Curing them is a client decision, not a migration: the
-- disclosed figures are the legally operative ones, so this reports, it does not rewrite.
--
--     SELECT disclosure_id, offer_id, application_id,
--            decision_event_id           AS offer_edge,
--            disclosure_decision_event_id AS record_edge
--     FROM v_disclosure_provenance
--     WHERE decision_event_id IS NOT NULL
--       AND disclosure_decision_event_id IS NOT NULL
--       AND decision_event_id <> disclosure_decision_event_id;
