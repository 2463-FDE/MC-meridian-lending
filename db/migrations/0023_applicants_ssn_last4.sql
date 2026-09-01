-- 0023_applicants_ssn_last4.sql — last-4 SSN column, reversible slice of D33.
--
-- Compose mounts db/init/* only, so this file exists for volumes already initialized
-- before this change. The column below is IDENTICAL to what db/init/001_schema.sql now
-- declares. Change both or neither. `applicants` has a single declaration site in init
-- (no CREATE TABLE in a migration, same as `payments` and `loans`), so there is no third
-- byte-identical copy to keep in sync.
--
-- WHY (docs/debt-log.md D33, docs/handoffs/2026-08-31-docs-glba-encryption-framing.md):
-- applicants.ssn is a plaintext TEXT column, and the GLBA Safeguards Rule (16 CFR
-- 314.4(c)(3)) requires customer information encrypted at rest with no qualifier the way
-- D35's "in transit" is qualified to external networks. Whether the full column can ever
-- be purged depends on a client answer (how long the platform must be able to re-run a
-- bureau pull) that has not landed yet. This migration ships only the part that does not
-- depend on that answer: a last-4 column kyc-service's presence check can use instead of
-- the full value, so the wire payload to kyc-service stops carrying the full SSN on the
-- recheck-kyc path today, before any retention decision is made. It does not touch
-- applicants.ssn and does not purge anything.
--
-- SAFE ON A POPULATED VOLUME. Added NULLABLE with no default, then backfilled from the
-- existing ssn column so a legacy application's recheck-kyc call is not silently
-- degraded -- a NULL last-4 would send kyc-service a falsy value where the full ssn used
-- to be truthy, changing ssn_verified for that one row. RIGHT(ssn, 4) mirrors
-- app/intake.py::ssn_last4's ssn[-4:] slicing, including the shorter-than-4 case (RIGHT
-- returns the whole string, same as a Python slice past the start of a short string).

ALTER TABLE applicants ADD COLUMN IF NOT EXISTS ssn_last4 TEXT;

UPDATE applicants
   SET ssn_last4 = RIGHT(ssn, 4)
 WHERE ssn_last4 IS NULL
   AND ssn IS NOT NULL
   AND ssn <> '';

-- IF NOT EXISTS swallows a pre-existing column of ANY type, which would let this
-- migration report success over a column the KYC call cannot use as a string. Assert
-- the definition, not the name -- and refuse rather than warn, same as 0014.
-- Resolved with to_regclass + pg_attribute, NOT information_schema: the ALTER and the
-- UPDATE above are unqualified and resolve by search_path, while an information_schema
-- lookup filtered on table_name alone matches EVERY schema holding an `applicants`. Under
-- `SET search_path TO other, public` this block could therefore grade a table the
-- statements above never touched, and SELECT ... INTO would silently take one row of
-- several rather than error. The check and the write must resolve the same table. Same
-- rule as migration 0020; debt D31 is the carded instance of getting this wrong.
DO $$
DECLARE
    actual text;
BEGIN
    SELECT format_type(a.atttypid, a.atttypmod) INTO actual
    FROM pg_attribute a
    WHERE a.attrelid = to_regclass('applicants')
      AND a.attname = 'ssn_last4'
      AND a.attnum > 0 AND NOT a.attisdropped;

    IF actual IS NULL THEN
        RAISE EXCEPTION 'applicants.ssn_last4 was not created';
    END IF;
    IF actual <> 'text' THEN
        RAISE EXCEPTION
            'applicants.ssn_last4 exists as % but must be text; '
            'resolve by hand -- this migration will not convert the column', actual;
    END IF;
END $$;
