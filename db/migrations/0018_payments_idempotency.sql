-- D19 (docs/debt-log.md): POST /payments has no idempotency key, so a retried or
-- double-clicked request inserts a second payments row and charges the card again.
-- Measured 2026-08-02: one $100 intent sent eight ways captured $800.00.
--
-- ADR 0013 Decision 1 and docs/specs/payments-week5.md D2 put the guarantee in the
-- SCHEMA rather than in a service, because two handlers write this table
-- (payment-service and servicing-service, debt D23) and a support engineer with psql
-- is a third. A control implemented in one handler silently does not apply to the other.
--
-- payments is created only in db/init/001_schema.sql -- no migration has ever held its
-- CREATE TABLE -- so the usual three-edit rule (init DDL + the original migration's
-- byte-identical CREATE TABLE + this file) collapses to two edits here. There is no
-- second CREATE TABLE to keep in step; test_payments_idempotency_ddl_parity asserts
-- this file and the init DDL still declare the same columns and indexes.

ALTER TABLE payments
  ADD COLUMN IF NOT EXISTS idempotency_key           TEXT,
  ADD COLUMN IF NOT EXISTS idempotency_expires_at    TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS request_fingerprint       TEXT,
  ADD COLUMN IF NOT EXISTS status                    TEXT NOT NULL DEFAULT 'captured',
  ADD COLUMN IF NOT EXISTS processor_idempotency_key TEXT,
  ADD COLUMN IF NOT EXISTS processor_ref             TEXT,
  ADD COLUMN IF NOT EXISTS amount_minor              BIGINT,
  ADD COLUMN IF NOT EXISTS updated_at                TIMESTAMPTZ;

-- ADD COLUMN IF NOT EXISTS on an EXISTING status column no-ops entirely, including its
-- NOT NULL DEFAULT 'captured' clause: a volume where status already exists nullable or
-- undefaulted keeps that shape after this statement reports success. Both insert paths
-- omit status, so those rows would land NULL instead of the legacy-compatible 'captured'.
-- Backfill before enforcing NOT NULL, then set the constraint and default explicitly --
-- these two ALTERs are unconditional (not IF NOT EXISTS) so they always bring an
-- already-existing column up to the same contract a fresh ADD COLUMN would have given it.
UPDATE payments SET status = 'captured' WHERE status IS NULL;
ALTER TABLE payments ALTER COLUMN status SET DEFAULT 'captured';
ALTER TABLE payments ALTER COLUMN status SET NOT NULL;

-- ADD COLUMN IF NOT EXISTS swallows a same-named column of ANY type: on a volume where
-- an operator or an earlier hand-applied attempt created `amount_minor` as TEXT, the
-- ALTER above reports success and every reader is handed a string. Assert each type and
-- RAISE EXCEPTION on a mismatch -- never a NOTICE, which would let the migration report
-- success over a column the service cannot use.
--
-- Tagged $mig$, not $$: the status-default check below compares against the literal
-- $$'captured'::text$$, and a nested dollar-quote with the SAME tag as the block that
-- contains it closes that block early. With both at $$ this DO body ended mid-expression
-- and the migration aborted at "syntax error at or near ::" -- taking the index-definition
-- block after it down too, so an operator ran a migration that reported a failure it
-- could not act on and verified nothing. Keep the outer tag distinct from any tag used
-- inside it.
DO $mig$
DECLARE
    expected CONSTANT text[][] := ARRAY[
        ['idempotency_key',           'text'],
        ['idempotency_expires_at',    'timestamp with time zone'],
        ['request_fingerprint',       'text'],
        ['status',                    'text'],
        ['processor_idempotency_key', 'text'],
        ['processor_ref',             'text'],
        ['amount_minor',              'bigint'],
        ['updated_at',                'timestamp with time zone']
    ];
    col  text;
    want text;
    got  text;
BEGIN
    FOR i IN 1 .. array_length(expected, 1) LOOP
        col  := expected[i][1];
        want := expected[i][2];
        -- table_schema is not optional: information_schema.columns spans EVERY
        -- schema, so an unqualified lookup can read a different `payments` table
        -- (another tenant schema, a leftover staging copy) and validate the wrong one.
        SELECT data_type INTO got
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'payments'
           AND column_name = col;
        IF got IS NULL THEN
            RAISE EXCEPTION
                'payments.% is missing after ADD COLUMN IF NOT EXISTS', col;
        END IF;
        IF got <> want THEN
            RAISE EXCEPTION
                'payments.% has data_type %, expected % -- a same-named column of the '
                'wrong type was already present and ADD COLUMN IF NOT EXISTS swallowed it',
                col, got, want;
        END IF;
    END LOOP;

    -- data_type alone does not prove the NOT NULL DEFAULT 'captured' contract: the
    -- ALTER COLUMN statements above are supposed to enforce it unconditionally, but
    -- assert the actual catalog state here too, so a future edit that drops or
    -- reorders those ALTERs is caught by this migration rather than by a NULL row on
    -- an insert path that omits status.
    DECLARE
        status_nullable text;
        status_default  text;
    BEGIN
        SELECT is_nullable, column_default INTO status_nullable, status_default
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'payments' AND column_name = 'status';
        IF status_nullable <> 'NO' THEN
            RAISE EXCEPTION
                'payments.status is nullable, expected NOT NULL -- both insert paths '
                'omit status and would write NULL rows instead of the legacy-compatible '
                'captured';
        END IF;
        -- information_schema.columns.column_default is the quoted+cast expression
        -- ('captured'::text), not the bare value -- an unanchored substring match (e.g.
        -- '%captured%') would also pass a wrong default like 'recaptured' or 'uncaptured',
        -- exactly the class of loose match this migration exists to refuse. Compare the
        -- FULL expression, since status's data_type is already asserted 'text' above and
        -- so is its rendering.
        IF status_default IS DISTINCT FROM $$'captured'::text$$ THEN
            RAISE EXCEPTION
                'payments.status has default %, expected captured', coalesce(status_default, '<none>');
        END IF;
    END;
END $mig$;

-- The Meridian key's arbiter. PARTIAL (WHERE idempotency_key IS NOT NULL) so the index
-- only covers rows that actually carry a key -- Postgres already treats every NULL as
-- distinct in a unique index, so pre-migration and retired-key rows (key set back to
-- NULL once its window passes on a terminal row) were never at risk of colliding with
-- each other even without the predicate. The partial form keeps the index small and, by
-- naming the predicate explicitly, matches the ON CONFLICT target the claim insert must
-- spell to let Postgres infer this arbiter. The index cannot be time-scoped: now() is
-- not immutable and cannot appear in a predicate, which is why the window is the
-- idempotency_expires_at column plus a retirement transition, not an index property.
CREATE UNIQUE INDEX IF NOT EXISTS payments_idempotency_key_uniq
  ON payments (idempotency_key) WHERE idempotency_key IS NOT NULL;

-- The processor key carries its OWN partial unique index, on the same principle: the
-- processor enforces uniqueness on its side, but money invariants live in the schema
-- here BECAUSE there are multiple writers. A drifted generator or a manual INSERT could
-- otherwise stamp one processor key onto two Meridian rows; the processor would collapse
-- the second charge as a replay while Meridian holds a second row it still applies.
-- This index makes that collision a refused write, before any processor call.
CREATE UNIQUE INDEX IF NOT EXISTS payments_processor_idempotency_key_uniq
  ON payments (processor_idempotency_key) WHERE processor_idempotency_key IS NOT NULL;

-- CREATE UNIQUE INDEX IF NOT EXISTS matches on NAME alone: a same-named index that is
-- non-unique, sits on the wrong column, or carries no predicate (or a different one)
-- makes the statement above a no-op that reports success, leaving the double-charge
-- control disabled while the migration says it shipped. Compare the DEFINITION.
DO $$
DECLARE
    expected CONSTANT text[][] := ARRAY[
        ['payments_idempotency_key_uniq',           'idempotency_key'],
        ['payments_processor_idempotency_key_uniq', 'processor_idempotency_key']
    ];
    idx      text;
    col      text;
    is_uniq  boolean;
    tbl      text;
    cols     text;
    pred     text;
BEGIN
    FOR i IN 1 .. array_length(expected, 1) LOOP
        idx := expected[i][1];
        col := expected[i][2];
        SELECT x.indisunique,
               c.relname,
               pg_get_expr(x.indpred, x.indrelid),
               (SELECT string_agg(a.attname, ',' ORDER BY k.ord)
                  FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord)
                  JOIN pg_attribute a
                    ON a.attrelid = x.indrelid AND a.attnum = k.attnum)
          INTO is_uniq, tbl, pred, cols
          FROM pg_index x
          JOIN pg_class i ON i.oid = x.indexrelid
          JOIN pg_namespace n ON n.oid = i.relnamespace
          JOIN pg_class c ON c.oid = x.indrelid
         -- relname is unique per SCHEMA, not per database. Without this an index of
         -- the same name in any other schema satisfies (or fails) the check, and the
         -- guard reports on an object this migration never touched.
         WHERE i.relname = idx AND n.nspname = current_schema();

        IF is_uniq IS NULL THEN
            RAISE EXCEPTION 'index % is missing after CREATE UNIQUE INDEX IF NOT EXISTS', idx;
        END IF;
        IF NOT is_uniq THEN
            RAISE EXCEPTION 'index % exists but is NOT unique -- it cannot arbitrate '
                            'ON CONFLICT and the double-charge control is disabled', idx;
        END IF;
        IF tbl <> 'payments' THEN
            RAISE EXCEPTION 'index % is on table %, expected payments', idx, tbl;
        END IF;
        IF cols IS DISTINCT FROM col THEN
            RAISE EXCEPTION 'index % covers column(s) %, expected %', idx, cols, col;
        END IF;
        -- The predicate is what makes the index partial, and the claim insert must
        -- spell the SAME predicate in its ON CONFLICT target or Postgres cannot infer
        -- the arbiter. A missing or different predicate here means the shipped insert
        -- raises "no unique or exclusion constraint matching the ON CONFLICT
        -- specification" on first use. Compare EXACTLY to pg_get_expr's stable
        -- "(col IS NOT NULL)" rendering -- a trailing wildcard ('%col IS NOT NULL%')
        -- would also pass a narrower, compound predicate like
        -- "col IS NOT NULL AND amount_minor > 0", which is a DIFFERENT (smaller) index
        -- than the arbiter the shipped ON CONFLICT names, so it cannot be inferred for
        -- every claimed row even though this LIKE check reports the index ready.
        IF pred IS DISTINCT FROM ('(' || col || ' IS NOT NULL)') THEN
            RAISE EXCEPTION
                'index % has predicate %, expected a partial index on (% IS NOT NULL)',
                idx, coalesce(pred, '<none>'), col;
        END IF;
    END LOOP;
END $$;
