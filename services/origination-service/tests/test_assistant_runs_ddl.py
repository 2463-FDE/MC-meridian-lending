"""assistant_runs DDL: the two declarations agree, and say what they must.

The table ships in db/init/001_schema.sql AND db/migrations/0021_assistant_runs.sql.
Anything that lets those two drift is the defect this file exists to catch: a volume
built from init and a volume migrated by hand would then disagree about what the write
path is allowed to store.
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
INIT_SQL = (REPO_ROOT / "db" / "init" / "001_schema.sql").read_text()
MIGRATION_SQL = (
    REPO_ROOT / "db" / "migrations" / "0021_assistant_runs.sql"
).read_text()

START = "-- >>> assistant_runs DDL"
END = "-- <<< assistant_runs DDL"


def _code(sql: str) -> str:
    """The executable SQL only, with `--` comments stripped.

    These files carry more prose than statements, and the prose quotes the very things
    being asserted against ("RAISE EXCEPTION, never RAISE NOTICE", "not
    current_schema()"). Matching raw text therefore reads a comment as if it were the
    statement it warns about — which is how the first version of this file passed a
    `RAISE NOTICE` assertion against a migration that contains no RAISE NOTICE at all.
    Line-based, which is sound here because no `--` appears inside a string literal.
    """
    return "\n".join(line.split("--")[0] for line in sql.splitlines())


def _block(sql: str) -> str:
    start, end = sql.index(START), sql.index(END)
    assert start < end, "parity markers out of order"
    return sql[start : end + len(END)]


def test_init_and_migration_ddl_are_identical():
    """Not 'both mention the table' — byte-identical between the parity markers.

    Bounded by markers rather than compared to end-of-file: the migration has to carry
    an assertion block AFTER the DDL (CREATE TABLE IF NOT EXISTS accepts a pre-existing
    table of any shape), which a marker-to-EOF comparison would forbid.
    """
    assert _block(INIT_SQL) == _block(MIGRATION_SQL)


def test_table_is_declared_in_exactly_two_places():
    """A third CREATE TABLE would reintroduce the drift the parity test rules out."""
    pattern = re.compile(r"CREATE TABLE IF NOT EXISTS assistant_runs\b")
    assert len(pattern.findall(INIT_SQL)) == 1
    assert len(pattern.findall(MIGRATION_SQL)) == 1


def test_application_id_carries_no_foreign_key():
    """The `not_found` population is exactly the rows whose id references nothing.

    A FK could only reject those rows or null the column, and either one destroys the
    signal a spike of requests against nonexistent ids carries. If someone "fixes" this
    by adding the reference, every refusal for a bogus id stops being recorded.
    """
    block = _code(_block(INIT_SQL))
    line = next(ln for ln in block.splitlines() if "application_id" in ln and "INTEGER" in ln)
    assert "REFERENCES" not in line.upper()
    assert "NOT NULL" in line.upper()


def test_refusal_code_is_constrained_to_the_codes_the_route_can_record():
    """A bare TEXT column invites `str(exc)`, which carries the app_id and provider text.

    The set is pinned here as well as in the DDL so adding a refusal branch in
    `_run_assistant` without widening the constraint fails a test rather than failing an
    INSERT at runtime, where the write is deliberately swallowed and the row is lost.
    """
    block = _code(_block(INIT_SQL))
    for code in (
        "not_found",
        "never_decisioned",
        "assistant_refused",
        "llm_unavailable",
        "kyc_blocked",
        "refused",
        "idempotency_conflict",
        "downstream_unavailable",
        "self_decision",
        "idempotency_key_too_long",
        "unknown_policy_topic",
    ):
        assert f"'{code}'" in block


def test_a_row_is_a_refusal_or_an_answer_and_never_both():
    """Keyed on http_status, NOT on `outcome` — see the DDL comment.

    `_charted` omits `outcome` when the served result does not carry it, so an
    outcome-keyed constraint would reject a legitimate row and (because the write
    swallows failures) lose it silently.
    """
    block = _code(_block(INIT_SQL))
    assert "CHECK ((http_status = 200) = (refusal_code IS NULL))" in block
    assert "outcome IS NULL" not in block


def test_migration_is_rerunnable():
    """The operator applies these by hand; a file that dies on its second run reports
    failure over a volume that is already correct."""
    code = _code(MIGRATION_SQL)
    assert "CREATE TABLE IF NOT EXISTS assistant_runs" in code
    assert code.count("CREATE INDEX IF NOT EXISTS") == 2
    assert "DROP TABLE" not in code


def test_migration_asserts_what_if_not_exists_swallowed():
    """RAISE EXCEPTION, never `RAISE NOTICE ... skipping`.

    CREATE TABLE IF NOT EXISTS accepts a pre-existing assistant_runs of any shape, so a
    table left by an earlier hand-applied attempt — or one whose constraint predates the
    never_decisioned split — satisfies it. Reporting success over that table is the
    failure the assertion block prevents.
    """
    guard = _code(MIGRATION_SQL[MIGRATION_SQL.index(END) :])
    assert "RAISE EXCEPTION" in guard
    assert "RAISE NOTICE" not in guard
    # convalidated: a constraint added NOT VALID enforces nothing on existing rows.
    assert "convalidated" in guard
    # By definition, not by name.
    assert "pg_get_constraintdef" in guard


def test_guard_resolves_the_same_table_the_insert_will():
    """to_regclass, not information_schema + current_schema().

    Under a search_path like `myapp, public` with the table in public, current_schema()
    is myapp: the lookup finds nothing, the guard passes vacuously, and the migration
    reports success on exactly the volume it exists to catch. Migration 0020 learned
    this the same way.
    """
    guard = _code(MIGRATION_SQL[MIGRATION_SQL.index(END) :])
    assert "to_regclass('assistant_runs')" in guard
    assert "current_schema()" not in guard


def test_table_is_not_append_only():
    """Deliberate contrast with decision_events, which carries a blocking trigger.

    This is telemetry, not a regulated record. Stated in a test so a reviewer reading the
    two tables side by side sees the asymmetry as a decision rather than an oversight.
    """
    assert "TRIGGER" not in _code(_block(INIT_SQL)).upper()
