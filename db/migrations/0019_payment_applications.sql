-- D3 (docs/debt-log.md): `balance.apply_payment` reads the balance, computes in Python and
-- writes it back, unlocked, on an autocommit connection. Two concurrent applies read the same
-- opening figure and the last writer wins. Measured 2026-08-02 against the live stack: one
-- $100 intent sent eight ways captured $800.00 and credited $600.00 — $200.00 taken and never
-- applied, every response 200. D19 (migration 0018) closed the eight charges; nothing has
-- closed the missing $200.
--
-- ADR 0020 and docs/specs/payments-week5.md D3(b)/D3(d) put the fix in the SCHEMA and in one
-- statement pair rather than in a service: two handlers move this balance (payment-service and
-- servicing-service, debt D23), so a lock taken in one does not apply to the other. UNIQUE
-- (payment_id) is the property no amount of application-level checking provides.
--
-- payment_applications is created here and in db/init/001_schema.sql. The two CREATE TABLE
-- blocks are byte-identical and test_payment_applications_ddl_parity asserts they stay so.

CREATE TABLE IF NOT EXISTS payment_applications (
    id           SERIAL PRIMARY KEY,
    loan_id      INTEGER NOT NULL REFERENCES loans(id),
    payment_id   INTEGER NOT NULL UNIQUE REFERENCES payments(id),
    amount_minor BIGINT NOT NULL,        -- integer minor units, the amount of record
                                         -- (ADR 0012); balances.balance stays float (D2)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Migration 0018 added payments.amount_minor but backfilled only `status`. Every pre-0018
-- row therefore carries amount_minor = NULL, including the seeded demo payment
-- (db/init/002_seed.sql), and the apply predicate below requires it NOT NULL — so without
-- this backfill every legacy payment is permanently ineligible to apply and the demo stack
-- cannot apply its own seeded payment. ADR 0020 records why the backfill is preferred over
-- leaving those rows ineligible.
--
-- `amount` is DOUBLE PRECISION. Cast to numeric BEFORE multiplying: float8 -> numeric goes
-- through the shortest round-trip decimal rendering, so 12.34 becomes exactly 12.34 and then
-- 1234, whereas (amount * 100)::bigint would truncate 12.34 * 100 = 1233.9999999999998 to
-- 1233 and lose a cent on the loan's ledger of record. ROUND, not truncation, for the same
-- reason servicing-service/app/payments.py::_amount_minor documents.
UPDATE payments
   SET amount_minor = ROUND(amount::numeric * 100)::bigint
 WHERE amount_minor IS NULL;

-- CREATE TABLE IF NOT EXISTS matches on NAME alone: on a volume where an operator or an
-- earlier hand-applied attempt created payment_applications with a different shape — a
-- nullable amount_minor, a TEXT amount_minor, or no UNIQUE on payment_id — the statement
-- above reports success and the control is disabled while the migration reports applied.
-- The UNIQUE is the whole replay guard and NOT NULL on amount_minor is what stops a NULL
-- credit, so assert both by DEFINITION and RAISE EXCEPTION on a mismatch. Never a NOTICE:
-- a migration that reports success over a table the service cannot trust is the failure
-- mode this block exists to refuse.
--
-- Tagged $mig$, not $$, and nothing nested inside uses $mig$ — migration 0018 closed its own
-- DO block early that way (fix 6ef566a) and took the block after it down with it.
DO $mig$
DECLARE
    expected CONSTANT text[][] := ARRAY[
        ['id',           'integer',                  'NO'],
        ['loan_id',      'integer',                  'NO'],
        ['payment_id',   'integer',                  'NO'],
        ['amount_minor', 'bigint',                   'NO'],
        ['created_at',   'timestamp with time zone', 'NO']
    ];
    col          text;
    want_type    text;
    want_notnull text;
    got_type     text;
    got_nullable text;
    uniq_count   integer;
BEGIN
    FOR i IN 1 .. array_length(expected, 1) LOOP
        col          := expected[i][1];
        want_type    := expected[i][2];
        want_notnull := expected[i][3];
        -- table_schema is not optional: information_schema.columns spans EVERY schema, so
        -- an unqualified lookup can validate a different payment_applications (another
        -- tenant schema, a leftover staging copy).
        SELECT data_type, is_nullable INTO got_type, got_nullable
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = 'payment_applications'
           AND column_name = col;
        IF got_type IS NULL THEN
            RAISE EXCEPTION
                'payment_applications.% is missing after CREATE TABLE IF NOT EXISTS -- a '
                'same-named table with a different shape was already present', col;
        END IF;
        IF got_type <> want_type THEN
            RAISE EXCEPTION
                'payment_applications.% has data_type %, expected %', col, got_type, want_type;
        END IF;
        IF got_nullable <> want_notnull THEN
            RAISE EXCEPTION
                'payment_applications.% is nullable, expected NOT NULL -- a nullable '
                'amount_minor or loan_id lets a row record an apply that credited nothing',
                col;
        END IF;
    END LOOP;

    -- The UNIQUE on payment_id is the replay guard: without it the same payment applies
    -- twice and credits twice, which is the defect one column over from the one this
    -- migration fixes. Probe the catalog for a unique index whose ONLY column is
    -- payment_id; relname is unique per schema, not per database, hence the nspname bound.
    SELECT count(*) INTO uniq_count
      FROM pg_index x
      JOIN pg_class i ON i.oid = x.indexrelid
      JOIN pg_namespace n ON n.oid = i.relnamespace
      JOIN pg_class c ON c.oid = x.indrelid
     WHERE c.relname = 'payment_applications'
       AND n.nspname = current_schema()
       AND x.indisunique
       AND x.indpred IS NULL
       AND (SELECT string_agg(a.attname, ',' ORDER BY k.ord)
              FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord)
              JOIN pg_attribute a
                ON a.attrelid = x.indrelid AND a.attnum = k.attnum) = 'payment_id';
    IF uniq_count = 0 THEN
        RAISE EXCEPTION
            'payment_applications has no unqualified UNIQUE index on payment_id -- a replay '
            'of the same payment would credit the balance a second time';
    END IF;

    -- The backfill above must leave no captured row unappliable. A NULL here after the
    -- UPDATE means `amount` itself was NULL, which the NOT NULL on payments.amount makes
    -- impossible -- so this firing means the UPDATE did not run (a reordered edit) rather
    -- than a data problem, and every legacy row would silently refuse to apply.
    IF EXISTS (SELECT 1 FROM payments WHERE amount_minor IS NULL) THEN
        RAISE EXCEPTION
            'payments.amount_minor is still NULL on % row(s) after the backfill -- those '
            'rows can never be applied by the D3 transaction',
            (SELECT count(*) FROM payments WHERE amount_minor IS NULL);
    END IF;
END
$mig$;
