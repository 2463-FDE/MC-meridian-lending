-- PR #7 review: decision retries must not append duplicate regulated events.
-- Optional caller-supplied request id; unique when present. A caller that retries a
-- timed-out POST with the same request_id gets the already-recorded decision back
-- instead of a second bureau pull + a second append-only event. A request WITHOUT a
-- request_id is an explicit re-decision (the audit-history path) — unchanged.

ALTER TABLE decision_events ADD COLUMN IF NOT EXISTS request_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_decision_events_request
    ON decision_events (request_id) WHERE request_id IS NOT NULL;
