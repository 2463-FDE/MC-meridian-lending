-- PR #7 review: decision retries must not append duplicate regulated events.
-- Optional caller-supplied request id; unique when present. A caller that retries a
-- timed-out POST with the same request_id gets the already-recorded decision back
-- instead of a second bureau pull + a second append-only event. A request WITHOUT a
-- request_id is an explicit re-decision (the audit-history path) — unchanged.

ALTER TABLE decision_events ADD COLUMN IF NOT EXISTS request_id TEXT;

-- Scoped to (app_id, request_id): a request_id replays only within its own
-- application. Reused on a different application it is an independent key (fresh
-- decision), never a replay of another application's record.
DROP INDEX IF EXISTS uq_decision_events_request;
CREATE UNIQUE INDEX IF NOT EXISTS uq_decision_events_request
    ON decision_events (app_id, request_id) WHERE request_id IS NOT NULL;
