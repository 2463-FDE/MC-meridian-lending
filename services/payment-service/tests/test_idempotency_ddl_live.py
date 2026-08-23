"""R-DDL / R-DDL2: the shipped SQL, against a real Postgres.

These are the two vectors the unit suite structurally cannot cover.

R-DDL. The arbiter is a PARTIAL unique index, so `ON CONFLICT (idempotency_key)`
without the predicate matches no arbiter and Postgres raises

    there is no unique or exclusion constraint matching the ON CONFLICT specification

at RUNTIME -- the claim insert failing on first use, which disables the whole
double-charge control. No amount of mocking catches that: a fake dispatches on the SQL
string and never parses it. So this executes the literal string the implementation
ships (`payments._CLAIM_SQL`, imported, not retyped -- a copy here could drift into
passing while the shipped one raises).

R-DDL2. Two rows carrying the same non-NULL processor_idempotency_key must be refused
locally, BEFORE any processor call, so a drifted generator or a manual INSERT is a
failed write rather than two Meridian rows the processor collapses into one charge.

Skipped, never silently passed, when no database is reachable: a vector that reports
success without connecting would be the "verifier reporting success for a path it
verified nothing on" failure these gates exist to prevent.
"""

import os
import uuid
from pathlib import Path

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from app.payments import _CLAIM_SQL  # noqa: E402  the SHIPPED string, not a copy

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "db" / "migrations" / "0018_payments_idempotency.sql"
INIT = REPO / "db" / "init" / "001_schema.sql"

DSN = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or ""


def _connect():
    if not DSN:
        pytest.skip(
            "no TEST_DATABASE_URL/DATABASE_URL set — R-DDL needs a real Postgres"
        )
    try:
        return psycopg2.connect(DSN, connect_timeout=3)
    except Exception as exc:
        pytest.skip(f"Postgres unreachable for R-DDL ({exc.__class__.__name__})")


@pytest.fixture
def schema():
    """A throwaway schema carrying the real payments DDL plus migration 0018."""
    conn = _connect()
    conn.autocommit = True
    name = "d19_" + uuid.uuid4().hex[:12]
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {name}")
        cur.execute(f"SET search_path TO {name}")
        # payments references loans(id); create the minimum this vector needs.
        cur.execute("CREATE TABLE loans (id SERIAL PRIMARY KEY)")
        body = INIT.read_text()
        start = body.index("CREATE TABLE IF NOT EXISTS payments (")
        cur.execute(body[start : body.index("\n);", start) + 3])
        cur.execute(MIGRATION.read_text())
    try:
        yield conn, name
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {name} CASCADE")
        conn.close()


def _claim_params(key):
    return (
        1,
        "4111111111111111",
        "123",
        250.0,
        25000,
        "card",
        key,
        24,
        "fp",
        uuid.uuid4().hex,
    )


def test_r_ddl_the_shipped_conflict_target_can_infer_the_partial_arbiter(schema):
    conn, name = schema
    key = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {name}")
        cur.execute("INSERT INTO loans DEFAULT VALUES")

        # First claim wins the key.
        cur.execute(_CLAIM_SQL, _claim_params(key))
        first = cur.fetchall()
        assert len(first) == 1, "the first claim must return its row"

        # Second claim of the same key returns zero rows -- claimed, not duplicated.
        cur.execute(_CLAIM_SQL, _claim_params(key))
        assert cur.fetchall() == [], "a claimed key must not insert a second row"

        cur.execute("SELECT count(*) FROM payments WHERE idempotency_key = %s", (key,))
        assert cur.fetchone()[0] == 1


def test_r_ddl_a_bare_conflict_target_raises_which_is_why_the_predicate_ships(schema):
    """The failure mode the predicate exists to avoid, pinned as a vector.

    If someone 'simplifies' the conflict target, this is the error their users get on
    the first charge.
    """
    conn, name = schema
    bare = _CLAIM_SQL.replace(
        "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING",
        "ON CONFLICT (idempotency_key) DO NOTHING",
    )
    assert bare != _CLAIM_SQL, "the shipped SQL no longer spells the index predicate"
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {name}")
        cur.execute("INSERT INTO loans DEFAULT VALUES")
        with pytest.raises(psycopg2.errors.InvalidColumnReference):
            cur.execute(bare, _claim_params(str(uuid.uuid4())))


def test_r_ddl2_a_duplicate_processor_key_is_refused_before_any_processor_call(schema):
    conn, name = schema
    processor_key = uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {name}")
        cur.execute("INSERT INTO loans DEFAULT VALUES")
        for i in range(2):
            params = list(_claim_params(str(uuid.uuid4())))
            params[9] = processor_key  # same processor key on two Meridian rows
            if i == 0:
                cur.execute(_CLAIM_SQL, tuple(params))
                assert len(cur.fetchall()) == 1
            else:
                with pytest.raises(psycopg2.errors.UniqueViolation):
                    cur.execute(_CLAIM_SQL, tuple(params))


def test_the_guards_read_their_own_schema_not_a_same_named_object_elsewhere(schema):
    """pg_class.relname and information_schema.columns are NOT database-unique.

    Found the hard way: a decoy schema holding a `payments` table and a NON-unique
    index called `payments_idempotency_key_uniq` made migration 0018 raise "exists but
    is NOT unique" while validating a schema it had never touched. The same
    unqualified lookup is in the services' readiness rungs, where it would report
    schema_not_ready over a perfectly good `payments`.

    This is reachable outside a test: any database carrying a second schema with a
    `payments` table -- a staging copy, a restored snapshot, a per-tenant schema.
    """
    conn, name = schema
    decoy = "decoy_" + uuid.uuid4().hex[:8]
    with conn.cursor() as cur:
        cur.execute(f"CREATE SCHEMA {decoy}")
        cur.execute(f"SET search_path TO {decoy}")
        cur.execute(
            "CREATE TABLE payments (id SERIAL PRIMARY KEY, idempotency_key TEXT, amount_minor TEXT)"
        )
        # Same NAME, wrong definition: non-unique, no predicate.
        cur.execute(
            "CREATE INDEX payments_idempotency_key_uniq ON payments (idempotency_key)"
        )
    try:
        with conn.cursor() as cur:
            # Re-run the migration over the REAL schema while the decoy exists. It must
            # validate its own objects and succeed.
            cur.execute(f"SET search_path TO {name}")
            cur.execute(MIGRATION.read_text())

            # And the readiness rung must agree, rather than reading the decoy.
            from app import config

            cur.execute(f"SET search_path TO {name}")
            ok, reason = config._payments_idempotency_ready(cur)
            assert ok is True, f"rung read the wrong schema: {reason}"
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA {decoy} CASCADE")
