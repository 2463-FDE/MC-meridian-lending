"""B1 (fix/ssn-at-rest review): a fresh volume must not seed a NULL ssn_last4.

Migration 0023 backfills ssn_last4 for volumes that predate the column, but a fresh
volume never runs migrations -- only db/init/*.sql. Neither 002_seed.sql's nor
003_seed_bulk.sql's `INSERT INTO applicants` names ssn_last4, so a natural-person row
lands with ssn truthy and ssn_last4 NULL. recheck_kyc
(app/routers/applications.py::recheck_kyc) sends only ssn_last4 to kyc-service, whose
CIP check is presence-only, so a NULL flips kyc_checks.ssn_verified to false on the
next recheck of a seeded application -- on a fresh volume only, never on one built by
replaying the migrations.

Grades the SQL as text, same as the no-sad vectors: no live Postgres needed to prove a
backfill statement is present and ordered after every applicant INSERT.

Ordering is graded at STATEMENT granularity, not file granularity: postgres's
docker-entrypoint-initdb.d runner executes each file's statements top-to-bottom, so a
backfill placed ahead of its own file's INSERT (same file, wrong order) still leaves
those rows NULL -- a file-index comparison alone cannot see that.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
INIT_DIR = REPO / "db" / "init"
INIT_FILES = sorted(INIT_DIR.glob("*.sql"))

_APPLICANTS_INSERT = re.compile(r"INSERT\s+INTO\s+applicants\b", re.IGNORECASE)
_SSN_LAST4_BACKFILL = re.compile(
    r"UPDATE\s+applicants\s+SET\s+ssn_last4\s*=\s*RIGHT\(ssn,\s*4\)", re.IGNORECASE
)


def _ordered_seed_text() -> str:
    """Every db/init file's SQL, concatenated in the filename order postgres runs
    them in. A single offset space lets a regex match's position compare across file
    boundaries, which comparing per-file indices cannot do."""
    return "\n".join(path.read_text() for path in INIT_FILES)


def test_every_applicants_insert_is_followed_by_the_backfill():
    seed_text = _ordered_seed_text()

    last_insert_end = None
    for m in _APPLICANTS_INSERT.finditer(seed_text):
        last_insert_end = m.end()
    assert last_insert_end is not None, (
        "no db/init file seeds applicants -- update this vector"
    )

    backfill_start = None
    for m in _SSN_LAST4_BACKFILL.finditer(seed_text):
        backfill_start = m.start()
    assert backfill_start is not None, (
        "no db/init file backfills ssn_last4 -- a fresh volume seeds natural-person "
        "applicants with ssn set and ssn_last4 NULL, which flips kyc_checks.ssn_verified "
        "to false on the next recheck_kyc call"
    )
    assert backfill_start > last_insert_end, (
        "the ssn_last4 backfill sits before the last `INSERT INTO applicants` in "
        "filename-execution order -- db/init statements run top-to-bottom within each "
        "file and file-by-file across files, so a backfill ahead of an applicants "
        "INSERT (even in the same file) runs against rows that don't exist yet and "
        "leaves them NULL"
    )


def test_the_backfill_mirrors_migration_0023s_null_guard():
    migration = (
        REPO / "db" / "migrations" / "0023_applicants_ssn_last4.sql"
    ).read_text()
    seed_text = "\n".join(p.read_text() for p in INIT_FILES)

    match = _SSN_LAST4_BACKFILL.search(seed_text)
    assert match, "no db/init file backfills ssn_last4"
    tail = seed_text[match.end() : match.end() + 200].lower()
    assert "ssn_last4 is null" in tail, (
        "backfill must guard on ssn_last4 IS NULL, same as migration 0023 -- otherwise "
        "it overwrites a value an earlier statement already set"
    )
    assert "ssn is not null" in tail and "ssn <> ''" in tail, (
        "backfill must guard on ssn being present, same as migration 0023 -- otherwise "
        "it writes a truthy last-4 for an entity applicant that has no ssn"
    )
    assert "right(ssn, 4)" in migration.lower(), migration  # sanity: the mirrored file
