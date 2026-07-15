-- ADR 0009 / ADR 0008: append-only decision-event record.
-- Additive only — touches no existing rows or columns. `decisions` remains the mutable
-- current-state pointer; decision_events is the system of record for Reg B adverse action.

CREATE TABLE IF NOT EXISTS decision_events (
    id                SERIAL PRIMARY KEY,
    app_id            INTEGER NOT NULL REFERENCES applications(id),
    outcome           TEXT NOT NULL,               -- approve | refer | deny | counteroffer
    principal_reasons JSONB NOT NULL,              -- [] for approve; [{code, reason}, ...] for deny/refer
    drivers           JSONB NOT NULL,              -- model score, ranked attributions, band cutoff, model id+version
    policy_band       TEXT NOT NULL,               -- band the score actually landed in
    inputs            JSONB NOT NULL,              -- identifier-free (ADR 0007 rule 1): no SSN/name/DOB/address/PAN
    decided_by        TEXT NOT NULL,               -- model id+version, or user id for manual/override decisions
    decided_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_decision_events_app ON decision_events(app_id);

-- Append-only enforced at the database, not just by code convention: the regulated
-- record must survive application bugs and ad-hoc SQL alike (contrast audit_logs, which
-- is mutable and soft-deletable — the debt this table is designed not to repeat).
CREATE OR REPLACE FUNCTION decision_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'decision_events is append-only (ADR 0009): % blocked', TG_OP;
END $$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_decision_events_append_only ON decision_events;
CREATE TRIGGER trg_decision_events_append_only
    BEFORE UPDATE OR DELETE ON decision_events
    FOR EACH ROW EXECUTE FUNCTION decision_events_append_only();
