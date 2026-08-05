-- 0013_loans_note_rate.sql — board servicing at the NOTE rate, not the disclosed APR.
--
-- Compose mounts db/init/* only, so this file exists for volumes already initialized
-- before this change. The column below is IDENTICAL to what db/init/001_schema.sql now
-- declares. Change both or neither. `loans` has a single declaration site in init (no
-- CREATE TABLE in a migration), so unlike disclosures there is no third byte-identical copy.
--
-- WHY: the disclosed payment schedule is amortized at the borrower's NOTE rate, because the
-- actuarial APR carries the prepaid origination fee and is therefore higher than the rate
-- interest accrues at (disclosure-service offers.py makes this explicit: on 15000/7.99/36 the
-- schedule at APR sums to 17442.72 against a disclosed total of 16919.15). Boarding stored
-- `offers.apr` into the single `loans.apr` column and servicing amortized its schedule at it,
-- so the funded loan's schedule contradicted its own TILA disclosure on every fee-bearing loan
-- (spec invariant D1: disclosed APR >= note rate for every non-zero-fee loan). This column
-- lets boarding store the note rate for servicing while `apr` keeps the disclosed APR for
-- display.
--
-- SAFE ON A POPULATED VOLUME. Added NULLABLE with no default: a loan boarded before this
-- migration has no recorded note rate, and inventing one would fabricate a contractual term.
-- Servicing falls back to `apr` for a NULL note_rate (legacy loan), which is the pre-change
-- behavior — no existing schedule changes. New loans always populate it (accept_offer derives
-- it from the delivered disclosure's compute_snapshot).

ALTER TABLE loans ADD COLUMN IF NOT EXISTS note_rate DOUBLE PRECISION;

-- IF NOT EXISTS swallows a pre-existing column of ANY type, which would let this migration
-- report success over a column the amortization path cannot use. Assert the definition, not
-- the name -- and refuse rather than warn, because origination's readiness rung reports ready
-- on the type and would then report ready over the wrong one.
DO $$
DECLARE
    actual text;
BEGIN
    SELECT data_type INTO actual
    FROM information_schema.columns
    WHERE table_name = 'loans' AND column_name = 'note_rate';

    IF actual IS NULL THEN
        RAISE EXCEPTION 'loans.note_rate was not created';
    END IF;
    IF actual <> 'double precision' THEN
        RAISE EXCEPTION
            'loans.note_rate exists as % but must be double precision; '
            'resolve by hand -- this migration will not convert the column', actual;
    END IF;
END $$;
