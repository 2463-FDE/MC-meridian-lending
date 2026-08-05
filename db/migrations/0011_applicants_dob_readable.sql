-- PR review: the DOB fix on fix/dob-validation is a request validator only, so it stops the
-- NEXT unreadable DOB but leaves one already in storage. Close the storage side too.
--
-- applicants.dob is a Postgres DATE, which reaches year 294276 and supports BC dates. Python's
-- date stops at 9999-12-31 and has no BC, so a value outside that window stores without
-- complaint and then raises "ValueError: year N is out of range" when a reader builds the
-- Python object. Both LOS read paths do exactly that:
--   * ORM -- models.Application.applicant is relationship(lazy="joined"), so the FULL Applicant
--     row (dob included) hydrates on every query that loads an Application: the officer list
--     (routers/applications.py::list_applications) and the detail view (::get_application).
--     One bad row therefore breaks the whole officer queue page it lands on, not just its own
--     application.
--   * raw psycopg2 -- run_kyc SELECTs ap.dob and calls .isoformat() on it
--     (routers/applications.py), and psycopg2 also converts DATE to a Python date.
-- A typed "21990-04-22" in the apply form's native date input produced this. The validator
-- cannot repair it, and non-API writers (seed scripts, operator SQL, a future importer) are not
-- behind the validator at all -- so the range belongs in the schema, where every writer meets it.
--
-- Bounds are the READABLE window, not a business rule. The 1900 floor stays in
-- schemas.py::_validate_dob: "implausibly old for a lending applicant" is an input-policy
-- decision that can change per product, while "Python cannot represent this date" is a fixed
-- availability limit of the readers. Keeping them separate means loosening the policy floor
-- never requires a migration. A future-dated dob is likewise validator-only: CHECK forbids
-- non-immutable expressions, so now()/CURRENT_DATE cannot appear here.
--
-- No separate startup row-scanner is added. A VALIDATED CHECK constraint cannot be created
-- while a violating row exists, so the constraint's PRESENCE is itself proof the table holds
-- no unreadable dob. origination-service /health checks pg_constraint for it and reports
-- schema_not_ready:ck_applicants_dob_readable (unhealthy, loud) until it is applied -- the same
-- readiness rung as applications.monthly_debt / uq_loans_app. One mechanism proves both that
-- the guard exists and that the legacy data is clean; a second scanner would only restate it.
--
-- LEGACY DATA (apply-order rung): an already-initialized volume may ALREADY hold an unreadable
-- dob -- written by the very gap this constraint closes. On such a volume this ALTER TABLE FAILS
-- with 'check constraint "ck_applicants_dob_readable" of relation "applicants" is violated by
-- some row', and /health keeps reporting schema_not_ready until the row is resolved. That is
-- intentional: repairing the value is a MANUAL, operator-run data decision on a KYC/CIP record,
-- not something a migration should do silently. Inspect and repair first -- cast to text so the
-- inspection itself does not go through a Python date converter:
--
--   -- inspect (psql, or any client reading the value as text)
--   SELECT id, name, dob::text
--   FROM applicants
--   WHERE dob IS NOT NULL AND (dob < DATE '0001-01-01' OR dob > DATE '9999-12-31');
--
--   -- then, per reviewed id: dob is nullable and an unreadable value carries no usable
--   -- information, so NULL is the lossless repair. Correct it to the real date instead when
--   -- the intended value is evident from the applicant's file (21990-04-22 -> 1990-04-22 is a
--   -- leading-digit typo from the native date input) -- prefer that, since NULL also means
--   -- "no DOB" to the CIP path.
--   UPDATE applicants SET dob = NULL WHERE id = <reviewed id>;
--
--   -- re-check CIP: kyc_checks.dob_verified for this applicant asserts a DOB was verified.
--   -- If the dob is now NULL, that assertion no longer has a value behind it and the
--   -- applicant needs re-verification (kyc_gate::require_kyc_passed blocks a natural person
--   -- whose dob_verified is false, so this fails closed, not open).
--   SELECT applicant_id, dob_verified FROM kyc_checks WHERE applicant_id = <reviewed id>;
--
-- A fresh db/init volume (and the seed, whose dobs are 1971-1992 plus NULL for the entity)
-- satisfies this cleanly. Re-running the migration is a no-op: only duplicate_object is
-- swallowed, so an existing constraint is fine while a violating row still raises.
--
-- SAME-NAME DRIFT (PR review): the constraint is declared twice -- here and in
-- db/init/001_schema.sql, which is what a fresh `make up` volume gets -- so "a constraint with
-- this name exists" and "the readable-range guard is in force" are not the same statement. A
-- name-only skip would silently accept a drifted or hand-created constraint with a weaker
-- expression, and origination-service /health would report ready over a column that still
-- takes a dob Python cannot represent. So the duplicate path compares the EXISTING definition
-- against the intended one and RAISES on any difference: an operator sees the mismatch instead
-- of a NOTICE that reads like success. Comparison ignores case, whitespace and parentheses
-- because pg_get_constraintdef re-renders from the parse tree (DATE '0001-01-01' comes back as
-- '0001-01-01'::date with the deparser's own parens); it does not ignore the bound literals.
-- Mirrors config.py::_normalize_constraint_def -- keep the three declarations in step.
-- One deparser detail the comparison has to account for: pg_get_constraintdef appends a
-- trailing NOT VALID to the definition of an unvalidated constraint, so a constraint whose
-- EXPRESSION is correct but which was added NOT VALID would otherwise fail the definition
-- comparison and be reported as drift -- sending the operator to DROP it when the correct
-- action is ALTER TABLE ... VALIDATE CONSTRAINT. The suffix is stripped before comparing so
-- each state gets its own message; convalidated is still checked, just separately.
DO $$
DECLARE
    expected TEXT := lower(regexp_replace(
        'CHECK (dob IS NULL OR (dob >= ''0001-01-01''::date AND dob <= ''9999-12-31''::date))',
        '[[:space:]()]', '', 'g'));
    raw_def TEXT;
    normalized TEXT;
    validated BOOLEAN;
BEGIN
    ALTER TABLE applicants
        ADD CONSTRAINT ck_applicants_dob_readable
        CHECK (dob IS NULL OR (dob >= DATE '0001-01-01' AND dob <= DATE '9999-12-31'));
EXCEPTION
    WHEN duplicate_object THEN
        SELECT pg_get_constraintdef(c.oid),
               regexp_replace(
                   lower(regexp_replace(pg_get_constraintdef(c.oid),
                                        '[[:space:]()]', '', 'g')),
                   'notvalid$', ''),
               c.convalidated
          INTO raw_def, normalized, validated
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
         WHERE c.conname = 'ck_applicants_dob_readable'
           AND t.relname = 'applicants'
           AND c.contype = 'c';
        IF normalized IS DISTINCT FROM expected THEN
            RAISE EXCEPTION 'ck_applicants_dob_readable already exists on applicants with a '
                            'different definition: %. Expected the readable-range check '
                            '(dob IS NULL OR dob BETWEEN 0001-01-01 AND 9999-12-31). Review '
                            'the existing constraint, then DROP and re-run this migration.',
                            coalesce(raw_def, '<not a CHECK constraint on applicants>');
        ELSIF NOT validated THEN
            RAISE EXCEPTION 'ck_applicants_dob_readable exists but is NOT VALID, so the rows '
                            'already stored were never checked. Run '
                            'ALTER TABLE applicants VALIDATE CONSTRAINT '
                            'ck_applicants_dob_readable (repair any violating row first -- see '
                            'the inspect/UPDATE queries above).';
        ELSE
            RAISE NOTICE 'ck_applicants_dob_readable already present, skipping';
        END IF;
END $$;
