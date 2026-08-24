-- D13a (docs/debt-log.md): payments.cvv stores sensitive authentication data.
--
-- PCI-DSS 3.2.1 prohibits retaining SAD after authorization outright. There is no
-- retention window, no compensating control, and no "we stopped writing it" variant of
-- compliance -- the remediation is a deletion of the values that are already stored.
-- ADR 0013 Decision 2 and docs/spec-payments-week5.md D4 decide it that way.
--
-- This migration is the CVV half (D13a). The PAN half (D13b: replace `pan` with
-- `card_token` + `card_brand` + `card_last4`) needs a tokenization provider and a card
-- entry surface that does not exist yet, so `pan` is deliberately untouched here and
-- stays open as D13b.
--
-- payments is created only in db/init/001_schema.sql -- no migration has ever held its
-- CREATE TABLE -- so the three-edit rule (init DDL + the original migration's
-- byte-identical CREATE TABLE + this file) collapses to two edits, the same as
-- migration 0018. test_no_sad.py asserts both declarations agree that the column is
-- gone.
--
-- ORDER MATTERS AND SO DOES THE REWRITE. Postgres DROP COLUMN does not erase anything:
-- it marks the attribute dropped in pg_attribute and leaves the bytes in every live row
-- version. The UPDATE below does not erase anything either -- an UPDATE writes a NEW row
-- version and leaves the OLD one, CVV intact, as a dead tuple until something rewrites
-- the heap. Either step alone (or both, without the rewrite) leaves every CVV readable
-- with pg_filedump, a raw file copy, or a restored backup. So: NULL, then DROP, then
-- rewrite the table.

-- 1. Clear the values while there is still a column to clear them through, then drop
--    the column -- both guarded on the column actually being there.
--
--    The guard is what makes this file re-runnable, and re-runnable is not a nicety
--    here: the operator applies these by hand. DROP COLUMN was already IF EXISTS, but
--    an unguarded `UPDATE payments SET cvv = NULL` aborts at "column cvv does not
--    exist" on any volume where the column is already gone -- a fresh schema built from
--    db/init/001_schema.sql, which no longer declares it, or a re-run after a partial
--    or hand-applied attempt. The file would then die at its first statement and never
--    reach the drop, the assertion, or the heap rewrite below, so the operator's second
--    attempt reports failure over a volume the rewrite still has to purge.
--
--    EXECUTE, not a static statement: PL/pgSQL resolves a static statement's columns the
--    first time that statement runs, which the IF already prevents, but dynamic SQL makes
--    the "never resolved when the column is absent" property independent of that.
DO $mig_purge$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name = 'payments'
       AND column_name = 'cvv'
  ) THEN
    EXECUTE 'UPDATE payments SET cvv = NULL WHERE cvv IS NOT NULL';
    EXECUTE 'ALTER TABLE payments DROP COLUMN cvv';
  END IF;
END
$mig_purge$;

-- 2. Assert the drop actually happened before the rewrite claims to have purged
--    anything. RAISE EXCEPTION, never NOTICE: a migration that reports success over a
--    column it did not remove is the failure this file exists to prevent.
DO $mig$
BEGIN
  IF EXISTS (
    SELECT 1 FROM information_schema.columns
     WHERE table_schema = current_schema()
       AND table_name = 'payments'
       AND column_name = 'cvv'
  ) THEN
    RAISE EXCEPTION
      'migration 0020: payments.cvv still exists after DROP COLUMN -- not purged';
  END IF;
END
$mig$;

-- 3. Rewrite the heap, which is what actually destroys the dead tuples holding the old
--    values. VACUUM cannot run inside a transaction block or a DO block, so this is a
--    bare top-level statement -- do not wrap this file in BEGIN/COMMIT.
--
--    VACUUM FULL takes an ACCESS EXCLUSIVE lock for its duration: payments is
--    unreadable and unwritable while it runs, so charges fail during the window. On a
--    volume where that downtime is not acceptable, run `pg_repack -t payments` instead
--    and skip this statement -- it reaches the same place online. docs/runbook.md
--    carries the operator procedure.
--
--    Re-running this migration on a volume whose column is already gone still performs
--    the rewrite. That is a wasted lock, not a defect, and it is the price of the file
--    staying idempotent.
VACUUM FULL payments;

-- NOT CLOSED BY THIS MIGRATION, and tracked rather than implied:
--   * WAL segments, replicas, and any backup taken before the rewrite still contain the
--     CVV values. Purging those is a retention action on the operator side, not a
--     schema change -- docs/debt-log.md D13 carries it under "Not covered".
--   * db/init/002_seed.sql's audit_logs row still holds a plaintext PAN in free text.
--     That is D20 (mutable audit_logs), a different entry with a different fix.
--   * The PAN column itself. D13b.
