-- 0016_provenance_disclosure_outcome.sql — expose the disclosure's own decision OUTCOME
-- in v_disclosure_provenance so a chain that cites a non-approving decision is detectable.
--
-- Compose mounts db/init/* only, so this file redefines the view for volumes already
-- initialized before this change. db/init/001_schema.sql and migration 0012 now carry the
-- SAME view body (they are byte-identical past the ADR 0012 marker — a test asserts it);
-- this migration reissues that body for a volume where 0015 already ran the previous view.
--
-- WHY: the view exposed `decision_outcome` from the OFFER's edge (offers.decision_event_id)
-- only. A disclosure is a regulated artifact for an APPROVED decision, and the create guard
-- now refuses a decision_event whose outcome is not 'approve'. But a back-book row written
-- before that guard — or an operator backfill of disclosures.decision_event_id — can carry a
-- deny/refer decision on the regulated record while every edge is non-null, and the offer's
-- edge (hence `decision_outcome`) is NULL on a legacy no-edge offer, so it cannot report the
-- disclosure's own outcome at all. The completeness check that walks this view then reports
-- chain_complete: true over a chain whose audit trail says it was not approved, and delivery
-- proceeds. Exposing disclosure_decision_outcome (walked through disclosures.decision_event_id)
-- lets the reader compare outcomes; disclosure-service treats a present, non-'approve' value as
-- an incomplete chain and refuses delivery. Boarding already re-checks the same outcome on its
-- own query.
--
-- SAFE ON A POPULATED VOLUME. A view holds no data; DROP + CREATE only changes the
-- projection. The added column is a plain projection of an existing column, no table rewrite.
--
-- disclosure-service /health probes v_disclosure_provenance for this column, so a deploy whose
-- code reads disclosure_decision_outcome before this migration runs reports
-- schema_not_ready:v_disclosure_provenance instead of 500-ing the provenance/delivery route.

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
