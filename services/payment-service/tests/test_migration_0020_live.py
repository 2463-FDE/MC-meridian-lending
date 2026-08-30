"""R-0020: migration 0020 is re-runnable, against a real Postgres.

The unit vectors in test_no_sad.py grade the migration as TEXT -- statement order, the
presence of a rewrite. Text cannot answer the one question an operator actually has:
does this file run twice without erroring? Migrations here are hand-applied with no
runner, so a second attempt after a partial one is the normal path, not an edge case.

The defect this vector holds down: an unguarded `UPDATE payments SET cvv = NULL` as the
file's first statement. On a volume where the column is already gone -- a fresh schema
built from db/init/001_schema.sql, or a re-run after a partial apply -- Postgres raises
`column "cvv" does not exist` and the file dies there, never reaching the DROP, the
verification block, or the VACUUM FULL that is the step which actually destroys the old
row versions. The operator's recovery attempt reports failure over a volume the rewrite
still has to purge.

Statements are sent one at a time, the way `psql -f` sends them: VACUUM FULL cannot run
inside a transaction block, and a multi-statement simple query is one implicit
transaction, so submitting the whole file in a single execute() fails on the rewrite for
a reason that has nothing to do with what is under test.

Skipped when no database is reachable -- a local developer without a stack running gets
no verdict rather than a false one. That skip is exactly what must NOT happen in CI: a
skipped vector exits 0, and a blocking gate reporting success for a path it never ran is
the failure these gates exist to prevent. So the CI step sets REQUIRE_LIVE_DB, which turns
every reason this file could skip -- no DSN, no psycopg2, an unreachable server -- into a
failure. A typo'd env var or a postgres service that never came up then fails the gate
instead of passing it silently.
"""

import os
import re
import uuid
from pathlib import Path

import pytest

# Set by the CI step that has a postgres service behind it. When it is set, this file has
# no skip path at all: every skip below becomes a failure.
REQUIRE_LIVE_DB = bool(os.getenv("REQUIRE_LIVE_DB"))


def _unavailable(reason: str):
    """Skip locally, fail in the gate that promised to run this."""
    if REQUIRE_LIVE_DB:
        pytest.fail(
            f"REQUIRE_LIVE_DB is set but the live vector could not run: {reason}. "
            "This gate blocks on migration 0020 being re-runnable; it must not pass "
            "without checking it."
        )
    pytest.skip(reason)


try:
    import psycopg2
except ImportError:  # pragma: no cover - exercised by the requirements install in CI
    psycopg2 = None

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "db" / "migrations" / "0020_payments_drop_cvv.sql"

DSN = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or ""

# A pre-0020 volume, reduced to what this migration touches: the column, a row holding a
# value, and nothing else. Building the real 001_schema.sql here would drag in every FK
# in the platform to test one ALTER.
_PAYMENTS_COLUMNS = (
    "(id SERIAL PRIMARY KEY, pan TEXT, cvv TEXT, amount DOUBLE PRECISION)"
)
_LEGACY_PAYMENTS = f"CREATE TABLE payments {_PAYMENTS_COLUMNS}"

# Postgres closes a dollar-quoted body at the first later occurrence of its OWN tag, so
# the tag has to be captured and matched, not just detected.
_DOLLAR_TAG = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")


def _is_only_comments(statement: str) -> bool:
    """A chunk with no executable line — the file's trailing `--` notes, which psql skips
    and psycopg2 rejects with "can't execute an empty query"."""
    return all(
        not line.strip() or line.strip().startswith("--")
        for line in statement.splitlines()
    )


def _split_statements(sql: str) -> list[str]:
    """Split on top-level semicolons, ignoring those inside a dollar-quoted body.

    Comments are left ON the statements rather than stripped: the migration's own
    RAISE EXCEPTION text contains a literal `--` inside a single-quoted string inside a
    dollar-quoted body, and a comment stripper that does not also track string literals
    would truncate that line and break the block."""
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    tag: str | None = None
    while i < len(sql):
        if tag is None:
            match = _DOLLAR_TAG.match(sql, i)
            if match:
                tag = match.group(0)
                buf.append(tag)
                i += len(tag)
                continue
            if sql[i] == ";":
                statement = "".join(buf).strip()
                if statement and not _is_only_comments(statement):
                    statements.append(statement)
                buf = []
                i += 1
                continue
        elif sql.startswith(tag, i):
            buf.append(tag)
            i += len(tag)
            tag = None
            continue
        buf.append(sql[i])
        i += 1
    tail = "".join(buf).strip()
    if tail and not _is_only_comments(tail):
        statements.append(tail)
    return statements


def _connect():
    if psycopg2 is None:
        _unavailable("psycopg2 is not installed")
    if not DSN:
        _unavailable("no TEST_DATABASE_URL/DATABASE_URL set")
    try:
        return psycopg2.connect(DSN, connect_timeout=5)
    except psycopg2.Error as exc:
        _unavailable(f"could not connect: {exc.__class__.__name__}")


def _apply_migration(cur) -> None:
    for statement in _split_statements(MIGRATION.read_text()):
        cur.execute(statement)


def _has_cvv(cur, schema: str) -> bool:
    cur.execute(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'payments' AND column_name = 'cvv'",
        (schema,),
    )
    return cur.fetchone() is not None


@pytest.fixture
def scratch_schema():
    """An isolated schema on the real database, dropped whatever the test does.

    The migration is written against current_schema(), so setting search_path is what
    points it at this throwaway copy of payments instead of the platform's own.
    """
    conn = _connect()
    conn.autocommit = True  # VACUUM FULL cannot run inside a transaction block
    schema = f"r0020_{uuid.uuid4().hex[:10]}"
    cur = conn.cursor()
    try:
        cur.execute(f'CREATE SCHEMA "{schema}"')
        cur.execute(f'SET search_path TO "{schema}"')
        yield cur, schema
    finally:
        try:
            cur.execute("SET search_path TO public")
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        finally:
            cur.close()
            conn.close()


def test_the_migration_purges_a_pre_0020_volume(scratch_schema):
    cur, schema = scratch_schema
    cur.execute(_LEGACY_PAYMENTS)
    cur.execute(
        "INSERT INTO payments (pan, cvv, amount) VALUES (%s, %s, %s)",
        ("4111111111111111", "123", 250.0),
    )
    assert _has_cvv(cur, schema)

    _apply_migration(cur)

    assert not _has_cvv(cur, schema), "0020 left the column in place"
    cur.execute("SELECT count(*) FROM payments")
    assert cur.fetchone()[0] == 1, (
        "0020 destroyed the payment rows, not just the column"
    )


def test_the_migration_reruns_clean_on_an_already_migrated_volume(scratch_schema):
    """The re-run an operator actually performs after a partial or failed apply."""
    cur, schema = scratch_schema
    cur.execute(_LEGACY_PAYMENTS)
    _apply_migration(cur)

    _apply_migration(cur)  # must not raise

    assert not _has_cvv(cur, schema)


def test_the_migration_runs_clean_on_a_fresh_schema_that_never_had_the_column(
    scratch_schema,
):
    """A volume built from the 001_schema.sql this PR ships never declares cvv at all."""
    cur, schema = scratch_schema
    cur.execute(
        "CREATE TABLE payments (id SERIAL PRIMARY KEY, pan TEXT, amount DOUBLE PRECISION)"
    )

    _apply_migration(cur)  # must not raise

    assert not _has_cvv(cur, schema)


def test_the_migration_purges_the_table_search_path_actually_resolves(scratch_schema):
    """current_schema() is not where `payments` necessarily lives.

    An operator applies this by hand, on whatever search_path their psql session carries.
    With `SET search_path TO empty_schema, data_schema`, current_schema() is the empty one
    while every unqualified `payments` in the file resolves to the other. A guard keyed on
    information_schema + current_schema() then finds no column, skips the purge, passes its
    own assertion vacuously and still runs the rewrite -- the file reports success over a
    table holding every CVV. The guard has to resolve the table the DML resolves.
    """
    cur, schema = scratch_schema
    data_schema = f"{schema}_data"
    cur.execute(f'CREATE SCHEMA "{data_schema}"')
    cur.execute(f'CREATE TABLE "{data_schema}".payments {_PAYMENTS_COLUMNS}')
    cur.execute(
        f'INSERT INTO "{data_schema}".payments (pan, cvv, amount) VALUES (%s, %s, %s)',
        ("4111111111111111", "123", 250.0),
    )
    # current_schema() is the (empty) scratch schema; `payments` resolves to the other one.
    cur.execute(f'SET search_path TO "{schema}", "{data_schema}"')

    _apply_migration(cur)

    assert not _has_cvv(cur, data_schema), (
        "the migration reported success while the payments table it actually resolves to "
        "still carries the CVV column and its values"
    )
    cur.execute(f'SELECT count(*) FROM "{data_schema}".payments')
    assert cur.fetchone()[0] == 1
