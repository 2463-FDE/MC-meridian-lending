-- D20: audit_logs was an ordinary, UPDATE/DELETE-able table with a deleted_at soft-delete
-- column -- forgeable, contradicting the README's "SOX-controlled with full audit" claim.
-- Additive only -- touches no existing rows or columns, mirrors decision_events (0004).

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
