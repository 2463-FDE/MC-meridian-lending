-- 0013_disclosures_document_body.sql — spec D6: delivery requires a recorded document.
--
-- Compose mounts db/init/* only, so this file exists for volumes that were already
-- initialized before this change. The column below is IDENTICAL to what
-- db/init/001_schema.sql now declares. Change both or neither.
--
-- 0012 also carries the column, in its CREATE TABLE. The two are not redundant and the
-- split is the same one 0012 itself describes: 0012 declares the table as it now stands, so
-- a volume that has never had `disclosures` gets the column with the table; this file adds
-- it to a volume where 0012 already ran and created the table without it. Both statements
-- are idempotent, so applying them in order on any volume converges on the same shape.
--
-- WHY: before this, `approved -> delivered` wrote a status and a timestamp and nothing
-- else. The borrower-facing document assembled at stage 3 lived only in the POST response,
-- so the compliance reviewer who approved and delivered it had no way to read it in a
-- later session, and the delivery transition asserted nothing about it. The document is
-- now persisted with the row it describes, and the lifecycle refuses to deliver a
-- disclosure that has none.
--
-- SAFE ON A POPULATED VOLUME. The column is added NULLABLE and there is no honest default:
-- a disclosure written before this migration has no recorded document, and inventing one
-- would be fabricating the evidence this column exists to hold. Existing DRAFT rows
-- therefore become undeliverable until they are regenerated -- deliberately, because the
-- alternative is delivering a document nobody can produce. Existing DELIVERED rows keep
-- NULL and stay frozen; the freeze trigger blocks any UPDATE of them, so no backfill of a
-- delivered row is possible even by hand.
--
-- NOT part of content_fingerprint. The fingerprint covers inputs + ruleset + outputs, and
-- every figure inside document_body is checked against those outputs before the row is
-- written (see disclosure-service `create_disclosure`). Folding model prose into a
-- regulated integrity hash would make the hash depend on wording.

ALTER TABLE disclosures ADD COLUMN IF NOT EXISTS document_body JSONB;

-- The IF NOT EXISTS above swallows a pre-existing column of ANY type, which would let this
-- migration report success over a column the write path cannot use (a TEXT document_body
-- accepts the JSON as a string and every read of it returns a string, not an object). Assert
-- the definition, not the name -- and refuse rather than warn, because the readiness rung in
-- disclosure-service reports ready on presence and would then report ready over it.
DO $$
DECLARE
    actual text;
BEGIN
    SELECT data_type INTO actual
    FROM information_schema.columns
    WHERE table_name = 'disclosures' AND column_name = 'document_body';

    IF actual IS NULL THEN
        RAISE EXCEPTION 'disclosures.document_body was not created';
    END IF;
    IF actual <> 'jsonb' THEN
        RAISE EXCEPTION
            'disclosures.document_body exists as % but must be jsonb; '
            'resolve by hand -- this migration will not convert a regulated column', actual;
    END IF;
END $$;
