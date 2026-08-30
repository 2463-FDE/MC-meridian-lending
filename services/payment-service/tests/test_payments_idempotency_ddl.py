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

import re
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
            # The predicate is the whole point: without it the index is not partial and
            # the shipped ON CONFLICT target -- which names this same predicate -- cannot
            # infer the arbiter. (Not because NULLs would collide: Postgres treats every
            # NULL as distinct in a unique index regardless of the partial predicate.)
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


def test_status_contract_probe_is_schema_qualified():
    """M1: information_schema.columns spans every schema, so the status NOT NULL/
    DEFAULT contract probe (the SELECT just above the RAISE EXCEPTIONs it guards)
    must filter on table_schema = current_schema(), same as the column-type loop
    a few lines above it and the index probe a few lines below -- otherwise a
    same-named payments.status column in another schema (a decoy, a restored
    snapshot, a per-tenant schema) is reachable by this exact query."""
    start = MIGRATION_SQL.index(
        "SELECT is_nullable, column_default INTO status_nullable, status_default"
    )
    end = MIGRATION_SQL.index(";", start)
    probe = MIGRATION_SQL[start:end]
    assert "table_schema = current_schema()" in probe


# --- the migration has to PARSE, not just say the right things -----------------
#
# Everything above is a text assertion over the two declarations, and 0018 passed all
# of it while being unable to execute at all: its verification block opened `DO $$` and
# then compared a column default against the literal `$$'captured'::text$$`. Postgres
# closes a dollar-quoted body at the first occurrence of its OWN tag, so the body ended
# mid-expression and psql reported `syntax error at or near "::"`. With ON_ERROR_STOP
# the migration aborted there, taking the index-definition block after it down too — so
# a hand-applied 0018 verified nothing and left the operator with a failure they could
# not act on.
#
# Nothing caught it: no CI job executes migration SQL (`.github/workflows/ci.yml` has no
# `psql` step and no postgres service), and `make prove` was satisfied by the text tests
# above, which read the file as a string. So the executability check is a text check too —
# it reproduces Postgres's dollar-quote lexing rule rather than needing a live database,
# and it sweeps every SQL file in the repo so the next migration cannot reintroduce it.

SQL_FILES = sorted(
    (REPO / "db" / "init").glob("*.sql")
) + sorted((REPO / "db" / "migrations").glob("*.sql"))


def _strip_line_comments(sql: str) -> str:
    """Blank out whole-line `--` comments, keeping line numbering intact.

    Only whole-line comments: a `--` after code could sit inside a string literal, and
    telling those apart needs the lexer this function exists to stand in for. Whole-line
    comments are the case that matters here, because the fix's own explanation quotes
    both `$mig$` and `$$'captured'::text$$` in prose and neither is a real tag.
    """
    out = []
    for line in sql.split("\n"):
        out.append("" if line.strip().startswith("--") else line)
    return "\n".join(out)


def _early_closed_do_blocks(sql: str) -> list[tuple[int, str]]:
    """Every `DO <tag>` block whose body is closed by a nested literal, not by its end.

    Postgres closes a dollar-quoted body at the FIRST later occurrence of the opening
    tag. A well-formed block's closing tag is therefore followed by `;` (optionally after
    whitespace) — `END $mig$;`. Anything else means the body ended early and the rest of
    the block is being parsed as ordinary SQL. Returns (line number, tag) per offender.
    """
    body = _strip_line_comments(sql)
    offenders = []
    for match in re.finditer(r"\bDO\s+(\$[A-Za-z_]*\$)", body):
        tag = match.group(1)
        close = body.find(tag, match.end())
        if close == -1:
            offenders.append((body.count("\n", 0, match.start()) + 1, tag))
            continue
        rest = body[close + len(tag) :].lstrip()
        if not rest.startswith(";"):
            offenders.append((body.count("\n", 0, close) + 1, tag))
    return offenders


def test_migration_0018_do_blocks_are_not_closed_by_a_nested_literal():
    """The D19 defect itself: `$$'captured'::text$$` inside a `DO $$` body."""
    offenders = _early_closed_do_blocks(MIGRATION_SQL)
    assert offenders == [], (
        "0018 has a DO block closed early by a nested dollar-quote with the same tag "
        f"(line, tag): {offenders}. Postgres ends the body at the first repeat of the "
        "tag, so the migration aborts with a syntax error and every check after it is "
        "skipped. Give the outer block a distinct tag, e.g. DO $mig$ ... END $mig$;"
    )


def test_no_sql_file_has_a_do_block_closed_by_a_nested_literal():
    """Repo-wide sweep. 0018 is the one that shipped broken; the rule holds for all of
    them, and no CI job executes this SQL, so this is the only thing standing between a
    nested tag and a hand-applied migration that aborts halfway."""
    assert SQL_FILES, "no SQL files found — the glob is wrong, not the tree"
    broken = {
        path.name: _early_closed_do_blocks(path.read_text())
        for path in SQL_FILES
        if _early_closed_do_blocks(path.read_text())
    }
    assert broken == {}, (
        f"DO block(s) closed early by a same-tagged nested dollar-quote: {broken}. "
        "Postgres closes the body at the first repeat of the tag."
    )
