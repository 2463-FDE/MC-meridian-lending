"""The D19 schema is declared twice and the two declarations must agree.

`payments` is created only in `db/init/001_schema.sql` — no migration has ever held its
`CREATE TABLE` — so the usual three-edit rule collapses to two: the init DDL (what a
fresh volume gets) and `db/migrations/0018_payments_idempotency.sql` (what an existing
volume gets). Nothing in the codebase compares them, and they drift silently: a fresh
`make up` and an upgraded volume would then carry different schemas, and the readiness
rung — which probes the LIVE catalog, not these files — reports ready on both.

That matters more here than for an ordinary column. The claim insert's `ON CONFLICT`
target has to match a PARTIAL unique index; if one declaration ships the index without
its `WHERE ... IS NOT NULL` predicate, Postgres cannot infer the arbiter and the first
charge on that volume raises, disabling the double-charge control at the moment it is
first needed.

These are text assertions over the real files, deliberately: the point is that the
DECLARATIONS agree, which no live-database test can establish (a live test sees one
schema, whichever the fixture built).
"""

from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
INIT_SQL = (REPO / "db" / "init" / "001_schema.sql").read_text()
MIGRATION_SQL = (
    REPO / "db" / "migrations" / "0018_payments_idempotency.sql"
).read_text()

# (column, the type spelling used in the init DDL, the information_schema data_type the
# migration asserts). Both spellings are checked so a change to either declaration that
# does not carry to the other fails here rather than in production.
IDEMPOTENCY_COLUMNS = (
    ("idempotency_key", "TEXT", "text"),
    ("idempotency_expires_at", "TIMESTAMPTZ", "timestamp with time zone"),
    ("request_fingerprint", "TEXT", "text"),
    ("status", "TEXT", "text"),
    ("processor_idempotency_key", "TEXT", "text"),
    ("processor_ref", "TEXT", "text"),
    ("amount_minor", "BIGINT", "bigint"),
    ("updated_at", "TIMESTAMPTZ", "timestamp with time zone"),
)

PARTIAL_UNIQUE_INDEXES = (
    ("payments_idempotency_key_uniq", "idempotency_key"),
    ("payments_processor_idempotency_key_uniq", "processor_idempotency_key"),
)


def _payments_table_ddl() -> str:
    """The CREATE TABLE payments block from the init DDL, up to its closing paren."""
    start = INIT_SQL.index("CREATE TABLE IF NOT EXISTS payments (")
    end = INIT_SQL.index("\n);", start)
    return INIT_SQL[start:end]


def test_every_idempotency_column_is_declared_in_both_places():
    table_ddl = _payments_table_ddl()
    for column, init_type, migration_type in IDEMPOTENCY_COLUMNS:
        assert column in table_ddl, (
            f"payments.{column} is in migration 0018 but not in the init DDL — "
            "a fresh volume would not have it"
        )
        assert init_type in table_ddl.split(column, 1)[1].split("\n", 1)[0], (
            f"payments.{column} is declared {init_type} in migration 0018 but the init "
            "DDL declares a different type on that line"
        )
        assert f"ADD COLUMN IF NOT EXISTS {column}" in MIGRATION_SQL, (
            f"payments.{column} is in the init DDL but migration 0018 does not add it — "
            "an existing volume would not get it"
        )
        assert f"'{migration_type}'" in MIGRATION_SQL, (
            f"migration 0018 does not assert the data_type of payments.{column}; "
            "ADD COLUMN IF NOT EXISTS swallows a same-named column of any type"
        )


def test_both_indexes_are_declared_partial_and_unique_in_both_places():
    for index, column in PARTIAL_UNIQUE_INDEXES:
        predicate = f"ON payments ({column}) WHERE {column} IS NOT NULL"
        for name, sql in (("init DDL", INIT_SQL), ("migration 0018", MIGRATION_SQL)):
            assert f"CREATE UNIQUE INDEX IF NOT EXISTS {index}" in sql, (
                f"{index} is not declared in the {name}"
            )
            # The predicate is the whole point: without it the index is not partial,
            # pre-0018 NULL rows collide, retirement cannot free a key, and the shipped
            # ON CONFLICT target cannot infer the arbiter.
            assert predicate in " ".join(sql.split()), (
                f"{index} in the {name} is missing its partial predicate "
                f"({predicate}) — the claim insert's ON CONFLICT would raise"
            )


def test_status_default_matches_what_pre_migration_rows_factually_are():
    """`status` is NOT NULL, so its DEFAULT is what every existing row becomes.

    'captured' is the only correct value: every pre-0018 row was inserted by a handler
    that charged the card and returned success. A different default would retroactively
    relabel the entire payment history, and — because only a terminal status releases an
    idempotency key — a non-terminal default would make every legacy row look like an
    intent still in flight.
    """
    table_ddl = _payments_table_ddl()
    assert "status      TEXT NOT NULL DEFAULT 'captured'" in table_ddl
    assert "status                    TEXT NOT NULL DEFAULT 'captured'" in MIGRATION_SQL
