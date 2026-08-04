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
DO $$
BEGIN
    ALTER TABLE applicants
        ADD CONSTRAINT ck_applicants_dob_readable
        CHECK (dob IS NULL OR (dob >= DATE '0001-01-01' AND dob <= DATE '9999-12-31'));
EXCEPTION
    WHEN duplicate_object THEN
        RAISE NOTICE 'ck_applicants_dob_readable already present, skipping';
END $$;
