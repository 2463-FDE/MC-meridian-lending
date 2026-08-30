"""Migration 0019 (D3 / ADR 0020) and the init DDL must declare one table.

`payment_applications` is created in two places: `db/init/001_schema.sql` (what a fresh
volume gets) and `db/migrations/0019_payment_applications.sql` (what an existing volume
gets). A difference between them means two databases built from this repository disagree
about the table the atomic apply writes — and the failure surfaces as a money bug on
whichever volume got the weaker shape, not as a migration error.

So the two CREATE TABLE blocks are compared BYTE-FOR-BYTE, not column by column: an
equality check cannot be satisfied by a declaration that merely looks similar, and it
catches a dropped NOT NULL or a changed default that a per-column loop would let through.
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
INIT_SQL = (REPO / "db" / "init" / "001_schema.sql").read_text()
MIGRATION_SQL = (
    REPO / "db" / "migrations" / "0019_payment_applications.sql"
).read_text()

CREATE = "CREATE TABLE IF NOT EXISTS payment_applications ("


def _table_ddl(sql: str) -> str:
    start = sql.index(CREATE)
    end = sql.index("\n);", start)
    return sql[start:end]


def test_the_two_declarations_are_byte_identical():
    assert _table_ddl(INIT_SQL) == _table_ddl(MIGRATION_SQL), (
        "db/init/001_schema.sql and db/migrations/0019_payment_applications.sql declare "
        "payment_applications differently — a fresh volume and a migrated one would not "
        "agree on the table the atomic apply writes"
    )


def test_the_replay_guard_is_declared_not_just_asserted():
    # UNIQUE (payment_id) is the property that makes a replay a no-op instead of a
    # second credit. The readiness rung probes for it, but the rung can only find what
    # the DDL creates.
    ddl = _table_ddl(INIT_SQL)
    assert "payment_id" in ddl and "UNIQUE" in ddl, ddl
    assert "amount_minor BIGINT NOT NULL" in ddl, (
        "a nullable amount_minor lets a row record an apply that credited nothing"
    )


def test_the_migration_asserts_the_definition_and_raises_on_a_mismatch():
    # CREATE TABLE IF NOT EXISTS matches on NAME alone, so the migration has to check
    # the shape it got and fail loudly. A NOTICE would let it report success over a
    # table the service cannot trust.
    assert "RAISE EXCEPTION" in MIGRATION_SQL
    assert "RAISE NOTICE" not in MIGRATION_SQL
    for data_type in ("'bigint'", "'integer'", "'timestamp with time zone'"):
        assert data_type in MIGRATION_SQL, (
            f"migration 0019 does not assert {data_type}; a same-named table of the "
            "wrong shape would be swallowed"
        )
    assert "indisunique" in MIGRATION_SQL, (
        "migration 0019 does not assert the UNIQUE index on payment_id"
    )


def test_the_migration_backfills_amount_minor_by_rounding_not_truncating():
    # Migration 0018 backfilled `status` but not `amount_minor`, so every pre-0018 row
    # is ineligible for the apply predicate until this runs — including the seeded demo
    # payment. int() on 12.34 * 100 = 1233.9999999999998 loses a cent, so the cast goes
    # through numeric and ROUNDs.
    assert "UPDATE payments" in MIGRATION_SQL
    assert "ROUND(amount::numeric * 100)::bigint" in MIGRATION_SQL, (
        "the backfill must round through numeric; a float multiply then truncate loses "
        "a cent on the loan's record of what was captured"
    )
    assert "WHERE amount_minor IS NULL" in MIGRATION_SQL, (
        "the backfill must not overwrite an amount_minor a capture already wrote"
    )
