"""Guards on the ADR 0012 provenance DDL that do NOT need a database.

Honest scope: these are text and wiring checks. The DDL's actual behaviour — the freeze
trigger, the check constraints, the unique index, the view's LEFT JOINs, and migration 0012
applying to a populated volume — was verified by applying both files to a throwaway
postgres:16-alpine (matching docker-compose.yml). That verification is not reproduced in
CI because no job here runs Postgres; the `make up` smoke step is where it recurs.

What these tests DO catch is the failure mode this repo has actually shipped before:
commit e0716da, where an index existed in a migration but not in the canonical init
schema, so a clean volume never got it and /health sat permanently schema_not_ready.
"""

import re
from pathlib import Path

import pytest

from app import config

REPO = Path(__file__).resolve().parents[3]
INIT_SQL = (REPO / "db" / "init" / "001_schema.sql").read_text()
MIGRATION_SQL = (REPO / "db" / "migrations" / "0012_disclosures.sql").read_text()
DOCUMENT_MIGRATION_SQL = (
    REPO / "db" / "migrations" / "0013_disclosures_document_body.sql"
).read_text()

# Objects the provenance chain needs. Named here so a dropped statement fails loudly.
REQUIRED_STATEMENTS = [
    "ALTER TABLE offers ADD COLUMN IF NOT EXISTS decision_event_id",
    "CREATE TABLE IF NOT EXISTS disclosures",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_disclosures_offer",
    "CREATE OR REPLACE FUNCTION disclosures_freeze_delivered()",
    "CREATE TRIGGER trg_disclosures_freeze_delivered",
    "CREATE TRIGGER trg_disclosures_no_truncate",
    "CREATE VIEW v_disclosure_provenance",
]


@pytest.mark.parametrize("statement", REQUIRED_STATEMENTS)
def test_init_schema_contains_statement(statement):
    assert statement in INIT_SQL


@pytest.mark.parametrize("statement", REQUIRED_STATEMENTS)
def test_migration_contains_statement(statement):
    """Compose mounts db/init/* only, so the migration exists for populated volumes.

    Both must carry the DDL — e0716da is what happens when only one does.
    """
    assert statement in MIGRATION_SQL


def test_init_and_migration_ddl_are_identical():
    """Not merely 'both mention the objects' — byte-identical DDL.

    The migration is the init DDL plus a header of apply-order notes. Anything else is
    drift waiting to happen.
    """
    marker = "-- ADR 0012 / spec D3"
    assert (
        INIT_SQL[INIT_SQL.index(marker) :]
        == MIGRATION_SQL[MIGRATION_SQL.index(marker) :]
    )


def test_offers_decision_event_id_is_nullable():
    """`ADD COLUMN ... NOT NULL` fails against a table that already holds rows.

    Nullability is what makes migration 0012 safe on a populated volume; the invariant is
    enforced at the write path instead. If someone tightens this, the migration breaks in
    production and passes on a fresh dev volume — the worst possible split.
    """
    statement = re.search(
        r"ALTER TABLE offers ADD COLUMN IF NOT EXISTS decision_event_id[^;]*;", INIT_SQL
    )
    assert statement, "decision_event_id column statement not found"
    assert "NOT NULL" not in statement.group(0)


def test_provenance_view_left_joins_from_offers():
    """A legacy offer with no decision event and no disclosure must still appear.

    An INNER JOIN here would silently drop exactly the rows with the worst provenance —
    the view would look clean by hiding its own subject matter.
    """
    view = MIGRATION_SQL[MIGRATION_SQL.index("CREATE VIEW v_disclosure_provenance") :]
    assert "FROM offers o" in view
    # Four: disclosures, the offer's decision event, the disclosure's OWN decision event
    # (0016, for disclosure_decision_outcome), and applications.
    assert view.count("LEFT JOIN") == 4
    # No plain/INNER JOIN survives once the LEFT JOINs are removed.
    assert "JOIN" not in view.replace("LEFT JOIN", "")


def test_provenance_view_exposes_no_direct_pii():
    """ADR 0007: identifiers only. Name, SSN, DOB and address must not cross into the view."""
    view = MIGRATION_SQL[MIGRATION_SQL.index("CREATE VIEW v_disclosure_provenance") :]
    for column in ("ssn", "dob", "address", "email", "phone", ".name"):
        assert column not in view.lower(), f"view exposes {column}"


def test_delivered_disclosures_are_frozen_not_append_only():
    """The trigger must fire on the delivered status only.

    Unconditional append-only would contradict the status machine — draft -> in_review ->
    approved are legitimate edits to a document the borrower has not seen.
    """
    assert "IF OLD.status = 'delivered' THEN" in INIT_SQL
    assert "BEFORE UPDATE OR DELETE ON disclosures" in INIT_SQL


def test_readiness_gate_covers_every_new_object():
    """/health must report schema_not_ready per object, so a code-ahead-of-migration
    deploy fails loudly instead of writing an offer with no provenance edge."""
    covered = {name for name, _ in config._SCHEMA_OBJECTS}
    assert covered == {
        "offers.decision_event_id",
        "disclosures",
        "uq_disclosures_offer",
        "trg_disclosures_freeze_delivered",
        "disclosures.document_body",
        "v_disclosure_provenance",
    }


def test_document_body_is_declared_in_init_and_in_its_migration():
    """Two declaration sites, same column. 0012 carries it in the CREATE TABLE (a volume
    that never had the table); 0013 adds it to a volume where 0012 already ran without it.
    A column present in only one place is e0716da again."""
    assert "document_body           JSONB," in INIT_SQL
    assert "document_body           JSONB," in MIGRATION_SQL
    assert "ADD COLUMN IF NOT EXISTS document_body JSONB" in DOCUMENT_MIGRATION_SQL


def test_document_body_migration_asserts_the_column_type():
    """`ADD COLUMN IF NOT EXISTS` swallows a pre-existing column of ANY type, so the
    migration would report success over a TEXT document_body — which accepts the JSON as a
    string and hands every reader back a string instead of an object. Compare the definition
    and RAISE, never warn-and-skip on the name."""
    assert "data_type" in DOCUMENT_MIGRATION_SQL
    assert "RAISE EXCEPTION" in DOCUMENT_MIGRATION_SQL
    assert "RAISE NOTICE" not in DOCUMENT_MIGRATION_SQL


def test_the_document_body_readiness_probe_asserts_jsonb():
    """A rung that checked only the column NAME would report ready over a TEXT column the
    write path cannot use — the same weaker-object-same-name gap the CHECK/index rungs close
    by asserting their definitions."""
    probe = dict(config._SCHEMA_OBJECTS)["disclosures.document_body"]
    assert "data_type = 'jsonb'" in probe


def test_readiness_probes_are_existence_queries():
    """Each probe must return a row when the object exists and none when it does not —
    _run_database_probe treats a missing row as not-ready."""
    for name, probe in config._SCHEMA_OBJECTS:
        assert probe.upper().startswith("SELECT 1 FROM"), name
        assert "WHERE" in probe.upper(), name
