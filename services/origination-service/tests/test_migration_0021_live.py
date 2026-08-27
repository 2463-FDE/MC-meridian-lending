"""R-0021: migration 0021 against a real Postgres.

`test_assistant_runs_ddl.py` grades the migration as TEXT — that the guard says RAISE
EXCEPTION, that the parity block matches the init DDL. Text cannot answer the questions
an operator actually has: does the file run twice, does the guard fire on the volume it
was written for, and do the CHECK constraints reject the rows they claim to reject. A
constraint nobody has ever seen refuse a row is a comment.

Sent as ONE execute(): unlike migration 0020 this file has no VACUUM, so nothing in it
objects to running inside the implicit transaction a multi-statement query creates, and
the statement splitter that file needs would be duplicated here for no gain.

Skipped when no database is reachable, so a developer with no stack gets no verdict
rather than a false one. That skip is exactly what must not happen in the gate: a skipped
test exits 0, and a blocking job reporting success over a path it never ran is the
failure these gates exist to prevent. REQUIRE_LIVE_DB turns every skip into a failure.
"""

import os
import uuid
from pathlib import Path

import pytest

REQUIRE_LIVE_DB = bool(os.getenv("REQUIRE_LIVE_DB"))


def _unavailable(reason: str):
    if REQUIRE_LIVE_DB:
        pytest.fail(
            f"REQUIRE_LIVE_DB is set but the live vector could not run: {reason}. "
            "This gate blocks on migration 0021; it must not pass without checking it."
        )
    pytest.skip(reason)


try:
    import psycopg2
except ImportError:  # pragma: no cover - exercised by the requirements install in CI
    psycopg2 = None

REPO = Path(__file__).resolve().parents[3]
MIGRATION = (REPO / "db" / "migrations" / "0021_assistant_runs.sql").read_text()
DSN = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or ""


@pytest.fixture()
def cur():
    """A private schema per test, with search_path pointed at it.

    Not merely isolation. The migration's guard resolves the table with to_regclass
    precisely so it grades whatever the DML will resolve to, and running every case under
    a non-default search_path is what makes that claim testable rather than asserted.
    """
    if psycopg2 is None:
        _unavailable("psycopg2 is not installed")
    if not DSN:
        _unavailable("no TEST_DATABASE_URL/DATABASE_URL set")
    try:
        conn = psycopg2.connect(DSN, connect_timeout=5)
    except psycopg2.Error as exc:
        _unavailable(f"could not connect: {exc.__class__.__name__}")
    conn.autocommit = True
    schema = f"mig0021_{uuid.uuid4().hex[:8]}"
    with conn.cursor() as c:
        c.execute(f"CREATE SCHEMA {schema}")
        c.execute(f"SET search_path TO {schema}")
        yield c
        c.execute(f"DROP SCHEMA {schema} CASCADE")
    conn.close()


def _served(refusal=None, status=200):
    return (
        "INSERT INTO assistant_runs (trace_id, application_id, task, http_status, "
        "refusal_code, latency_ms) VALUES (%s, %s, %s, %s, %s, %s)",
        ("trace-1", 42, "explain", status, refusal, 12),
    )


def test_migration_applies_and_is_rerunnable(cur):
    """Applied by hand with no runner, so a second attempt after a partial one is the
    normal path. A file that dies on re-run reports failure over a correct volume."""
    cur.execute(MIGRATION)
    cur.execute(MIGRATION)
    cur.execute("SELECT to_regclass('assistant_runs')")
    assert cur.fetchone()[0] is not None


def test_the_guard_fires_on_a_table_create_if_not_exists_would_have_accepted(cur):
    """The defect this vector holds down.

    CREATE TABLE IF NOT EXISTS accepts a pre-existing assistant_runs of ANY shape. A
    hand-applied earlier attempt whose CHECK predates the never_decisioned split
    satisfies it, and without the assertion block the migration reports success over a
    column that admits codes no reader recognises.
    """
    cur.execute(
        "CREATE TABLE assistant_runs ("
        "  id BIGSERIAL PRIMARY KEY, trace_id TEXT NOT NULL,"
        "  application_id INTEGER NOT NULL, task TEXT NOT NULL, policy_topic TEXT,"
        "  http_status INTEGER NOT NULL, refusal_code TEXT, outcome TEXT,"
        "  record_status TEXT, policy_band TEXT, narration_validated BOOLEAN,"
        "  policy_citations INTEGER, policy_searches INTEGER,"
        "  latency_ms INTEGER NOT NULL,"
        "  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),"
        "  CONSTRAINT ck_assistant_runs_task CHECK (task IN ('decision','explain')),"
        "  CONSTRAINT ck_assistant_runs_refusal_code"
        "    CHECK (refusal_code IS NULL OR refusal_code IN ('not_found')),"
        "  CONSTRAINT ck_assistant_runs_refusal_matches_status"
        "    CHECK ((http_status = 200) = (refusal_code IS NULL))"
        ")"
    )
    with pytest.raises(psycopg2.errors.RaiseException) as raised:
        cur.execute(MIGRATION)
    assert "ck_assistant_runs_refusal_code" in str(raised.value)


def test_a_not_valid_constraint_does_not_satisfy_the_guard(cur):
    """`convalidated` is load-bearing: ADD CONSTRAINT ... NOT VALID installs the guard for
    future writes while explicitly not checking the rows already there."""
    cur.execute(MIGRATION)
    cur.execute(
        "ALTER TABLE assistant_runs DROP CONSTRAINT ck_assistant_runs_refusal_code"
    )
    cur.execute(
        "ALTER TABLE assistant_runs ADD CONSTRAINT ck_assistant_runs_refusal_code "
        "CHECK (refusal_code IS NULL OR refusal_code IN "
        "('not_found','never_decisioned','downstream_unavailable','idempotency_conflict'))"
        " NOT VALID"
    )
    with pytest.raises(psycopg2.errors.RaiseException) as raised:
        cur.execute(MIGRATION)
    assert "NOT VALID" in str(raised.value)


def test_an_unknown_refusal_code_is_rejected(cur):
    """The column is CHECK-constrained rather than TEXT so `str(exc)` cannot land in it —
    an httpx message embeds the request URL, which embeds the app_id."""
    cur.execute(MIGRATION)
    sql, params = _served(refusal="HTTPStatusError: GET /decisions/42/record", status=502)
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(sql, params)


def test_a_refusal_cannot_be_recorded_as_a_success(cur):
    """A row is a served answer or a refusal, never ambiguously both."""
    cur.execute(MIGRATION)
    sql, params = _served(refusal="not_found", status=200)
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(sql, params)
    sql, params = _served(refusal=None, status=404)
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(sql, params)


def test_the_rows_the_route_actually_writes_are_accepted(cur):
    """The mirror of the refusals above: every code `_run_assistant` can record inserts."""
    cur.execute(MIGRATION)
    sql, params = _served()
    cur.execute(sql, params)
    for code in (
        "not_found",
        "never_decisioned",
        "assistant_refused",
        "llm_unavailable",
        "kyc_blocked",
        "refused",
        "idempotency_conflict",
        "downstream_unavailable",
    ):
        sql, params = _served(refusal=code, status=502)
        cur.execute(sql, params)
    cur.execute("SELECT count(*) FROM assistant_runs")
    assert cur.fetchone()[0] == 9


def test_an_application_id_with_no_application_is_accepted(cur):
    """No foreign key, deliberately: the `not_found` population is exactly the rows whose
    id references nothing, and a FK could only reject them or null the column."""
    cur.execute(MIGRATION)
    sql, params = _served(refusal="not_found", status=404)
    cur.execute(sql, params)
    cur.execute("SELECT application_id FROM assistant_runs")
    assert cur.fetchone()[0] == 42
