-- Production evals for the officer assistant: one durable row per request.
--
-- The assistant is observable one run at a time and not in aggregate. `assistant.entry`
-- and `assistant.request` carry the outcome enums already, but LangSmith only holds the
-- runs that were exported -- `trace()` is a no-op unless LANGSMITH_TRACING is set -- so no
-- rate computed from it has a trustworthy denominator. `assistant.run()` itself writes
-- nothing to this database at all. This table is that denominator.
--
-- assistant_runs is created here and in db/init/001_schema.sql, and nowhere else. The
-- block between the parity markers below is byte-identical to the one in the init DDL;
-- tests/test_assistant_runs_ddl.py compares them directly, so the three-edit rule
-- (init DDL + original migration's CREATE TABLE + a new ALTER migration) collapses to
-- two edits here, as it did for 0018 and 0020 -- this file IS the original migration.
--
-- Re-runnable by construction: CREATE TABLE IF NOT EXISTS and CREATE INDEX IF NOT EXISTS
-- are no-ops on a volume that already has them, and the assertion block at the end
-- inspects rather than mutates. The operator applies these by hand, so a file that dies
-- on its second run reports failure over a volume that is in fact correct.

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

-- Assert what CREATE TABLE IF NOT EXISTS silently accepted.
--
-- IF NOT EXISTS swallows a pre-existing assistant_runs of ANY shape: a table created by
-- an earlier hand-applied attempt, or one whose ck_assistant_runs_refusal_code predates
-- the never_decisioned split, satisfies it and this file would otherwise report success
-- over a column that admits whatever a caller writes. RAISE EXCEPTION, never
-- `RAISE NOTICE ... skipping`: a migration that reports success over an object it did
-- not actually create is the failure this block exists to prevent.
--
-- to_regclass('assistant_runs'), not information_schema + current_schema(): the guard has
-- to ask about the SAME table the service's INSERT will resolve to, and that resolves by
-- search_path. Under a search_path like `myapp, public` with the table in public,
-- current_schema() is myapp, the lookup finds nothing, and the assertion passes
-- vacuously on exactly the volume it exists to catch. Same rule as migration 0020.
--
-- The columns are asserted too, not just the constraints. A pre-existing table missing
-- policy_searches, or carrying latency_ms as TEXT, satisfies CREATE TABLE IF NOT EXISTS
-- and every constraint check below, and then fails every INSERT in
-- services/origination-service/app/assistant_runs.py -- which swallows the error, by
-- design, so a telemetry fault cannot 500 an officer's answer. The result is a table that
-- exists, a migration that reported success, a healthy /health, and no rows at all.
--
-- id and created_at are asserted although the INSERT does not name them: both are NOT
-- NULL, so an omitted column with no default fails the write exactly as a missing one
-- does. format_type is the rendering pg_attribute reports, and attnotnull/atthasdef are
-- the two properties that decide whether the write may leave a column out.
--
-- The CHECK definitions are compared by CONTENT, not merely by name, and `convalidated`
-- is required so a constraint added NOT VALID cannot pass. Postgres rewrites and
-- re-parenthesizes a CHECK when it stores it (`IN (...)` comes back as
-- `= ANY (ARRAY[...])`), so the comparison strips whitespace and parentheses and
-- lowercases both sides -- but it compares the WHOLE expression. A substring test asks
-- only whether the constraint mentions a code, and a WIDER constraint passes that just as
-- easily as the intended one -- which is how `str(exc)` reaches a column that was
-- CHECK-constrained precisely to keep it out.
DO $mig$
DECLARE
  bad_columns    text;
  extra_required text;
  actual         text;
  r              record;
BEGIN
  IF to_regclass('assistant_runs') IS NULL THEN
    RAISE EXCEPTION
      'migration 0021: assistant_runs does not resolve on the search_path after CREATE';
  END IF;

  SELECT string_agg(
           format('%s (expected %s, not null %s, default %s; found %s)',
                  e.name, e.typ, e.not_null, e.has_default,
                  coalesce(f.typ || ', not null ' || f.not_null || ', default ' || f.has_default,
                           'no such column')),
           '; ' ORDER BY e.name)
    INTO bad_columns
    FROM (VALUES
      ('id'::text,            'bigint'::text,             true,  true),
      ('trace_id',            'text',                     true,  false),
      ('application_id',      'integer',                  true,  false),
      ('task',                'text',                     true,  false),
      ('policy_topic',        'text',                     false, false),
      ('http_status',         'integer',                  true,  false),
      ('refusal_code',        'text',                     false, false),
      ('outcome',             'text',                     false, false),
      ('record_status',       'text',                     false, false),
      ('policy_band',         'text',                     false, false),
      ('narration_validated', 'boolean',                  false, false),
      ('policy_citations',    'integer',                  false, false),
      ('policy_searches',     'integer',                  false, false),
      ('latency_ms',          'integer',                  true,  false),
      ('created_at',          'timestamp with time zone', true,  true)
    ) AS e(name, typ, not_null, has_default)
    LEFT JOIN (
      SELECT a.attname                            AS name,
             format_type(a.atttypid, a.atttypmod) AS typ,
             a.attnotnull                         AS not_null,
             a.atthasdef                          AS has_default
        FROM pg_attribute a
       WHERE a.attrelid = to_regclass('assistant_runs')
         AND a.attnum > 0
         AND NOT a.attisdropped
    ) AS f ON f.name = e.name
   WHERE f.name IS NULL
      OR f.typ <> e.typ
      OR f.not_null <> e.not_null
      OR f.has_default <> e.has_default;
  IF bad_columns IS NOT NULL THEN
    RAISE EXCEPTION
      'migration 0021: assistant_runs does not have the shape the INSERT in services/origination-service/app/assistant_runs.py writes: %',
      bad_columns;
  END IF;

  -- An extra column is harmless unless the write has to supply it: NOT NULL, no default,
  -- and absent from the INSERT column list fails every row. The list below is that column
  -- list, restricted to the columns the write always supplies a value for.
  SELECT string_agg(a.attname, ', ' ORDER BY a.attname)
    INTO extra_required
    FROM pg_attribute a
   WHERE a.attrelid = to_regclass('assistant_runs')
     AND a.attnum > 0
     AND NOT a.attisdropped
     AND a.attnotnull
     AND NOT a.atthasdef
     AND a.attname NOT IN (
       'trace_id', 'application_id', 'task', 'http_status', 'latency_ms'
     );
  IF extra_required IS NOT NULL THEN
    RAISE EXCEPTION
      'migration 0021: assistant_runs requires columns the INSERT does not write: %',
      extra_required;
  END IF;

  FOR r IN
    SELECT *
      FROM (VALUES
        ('ck_assistant_runs_task'::text,
         'checktask=anyarray[''decision''::text,''explain''::text]'::text),
        ('ck_assistant_runs_refusal_code',
         'checkrefusal_codeisnullorrefusal_code=anyarray[''not_found''::text,'
         '''never_decisioned''::text,''assistant_refused''::text,'
         '''llm_unavailable''::text,''kyc_blocked''::text,''refused''::text,'
         '''idempotency_conflict''::text,''downstream_unavailable''::text]'),
        ('ck_assistant_runs_refusal_matches_status',
         'checkhttp_status=200=refusal_codeisnull')
      ) AS e(conname, expected)
  LOOP
    SELECT lower(regexp_replace(pg_get_constraintdef(oid), '[\s()]', '', 'g'))
      INTO actual
      FROM pg_constraint
     WHERE conrelid = to_regclass('assistant_runs')
       AND conname = r.conname
       AND contype = 'c'
       AND convalidated;
    IF actual IS NULL THEN
      RAISE EXCEPTION 'migration 0021: % is missing or NOT VALID', r.conname;
    END IF;
    IF actual <> r.expected THEN
      RAISE EXCEPTION
        'migration 0021: % does not match the intended expression (found %)',
        r.conname, actual;
    END IF;
  END LOOP;
END
$mig$;

-- NOT CLOSED BY THIS MIGRATION, tracked rather than implied:
--   * Retention. These rows are applicant-linkable through application_id and accumulate
--     without bound; no retention policy exists yet (docs/debt-log.md D5 still carries
--     the rotation/retention row). The aggregate CLI is the export boundary -- it emits
--     counts, never application_id or trace_id -- but the table itself keeps them.
--   * steps_used and `scored` are computed inside assistant.run() and never returned, so
--     they are not persisted here. Adding them would change the officer response shape
--     that the trace-surface work deliberately left alone.
--   * Token usage and cost. Already on the `llm.transport` span, where LangSmith computes
--     cost from it; duplicating it into this row buys no new capability.
