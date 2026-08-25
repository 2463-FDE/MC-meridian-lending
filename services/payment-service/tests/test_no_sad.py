"""D13a: the CVV column is deleted, not merely unwritten.

PCI-DSS 3.2.1 prohibits retaining sensitive authentication data after authorization
outright — there is no compensating control and no retention window. So the remediation
is a deletion, and "we stopped writing it" does not satisfy it: the stored values are
still there.

These vectors hold the four places the deletion has to be true at once, because each one
alone leaves the data reachable:

  DDL          a fresh volume must not create the column at all
  migration    an existing volume must NULL the values, drop the column, and REWRITE the
               table — Postgres DROP COLUMN only marks the attribute dropped, and the
               preceding UPDATE leaves the old row versions as dead tuples, so without a
               rewrite every CVV is still recoverable from the data files
  code         neither handler may name the column, and the request model must not carry
               the field
  readiness    a volume that still HAS the column must report unhealthy rather than
               serve charges over it, because migrations here are hand-applied and lag
               the init DDL

The redactor backstop stays and is asserted here too: a stale caller can still put a cvv
key in a request body, and that body reaches a log formatter before anything parses it.

Kept in step with servicing-service's copy — both services write this table (debt D23).
"""

import inspect
import json
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config, payments
from app.config import _no_stored_sad_ready
from app.logging_config import get_logger
from app.schemas import PaymentIn
from app.main import app

REPO = Path(__file__).resolve().parents[3]
INIT_SQL = (REPO / "db" / "init" / "001_schema.sql").read_text()
# EVERY file docker-entrypoint runs on a fresh volume, not just 002_seed.sql: 003_seed_bulk
# also inserts into payments, and it named the cvv column -- on a volume built from the
# 001_schema.sql that no longer declares it, that aborts the whole init at
# `column "cvv" of relation "payments" does not exist`, so the fresh stack has no bulk seed.
INIT_FILES = sorted((REPO / "db" / "init").glob("*.sql"))
MIGRATION = REPO / "db" / "migrations" / "0020_payments_drop_cvv.sql"

# The column list of an `INSERT INTO payments (...)`. Group 1 is empty when the statement
# has no column list at all -- a positional insert, which would put a value into whatever
# column sits at that ordinal and cannot be graded by name.
_PAYMENTS_INSERT = re.compile(
    r"INSERT\s+INTO\s+payments\s*(?:\(([^)]*)\))?", re.IGNORECASE
)


def _payments_table_ddl() -> str:
    start = INIT_SQL.index("CREATE TABLE IF NOT EXISTS payments (")
    end = INIT_SQL.index("\n);", start)
    return INIT_SQL[start:end]


def _migration_statements() -> str:
    """The migration's executable SQL, lowercased, with `--` comments stripped.

    The comments in that file discuss DROP COLUMN and the rewrite at length, so a raw
    substring search finds prose and orders it against prose. Only the statements decide
    what the database does."""
    lines = []
    for line in MIGRATION.read_text().splitlines():
        head = line.split("--", 1)[0]
        if head.strip():
            lines.append(head)
    return "\n".join(lines).lower()


# --- the declaration -----------------------------------------------------------------


def test_the_init_ddl_declares_no_cvv_column():
    assert "cvv" not in _payments_table_ddl().lower(), (
        "db/init/001_schema.sql still declares payments.cvv — a fresh `make up` would "
        "create a column PCI-DSS 3.2.1 prohibits"
    )


def test_no_init_file_seeds_a_cvv():
    seen = 0
    for path in INIT_FILES:
        for match in _PAYMENTS_INSERT.finditer(path.read_text()):
            seen += 1
            columns = match.group(1)
            assert columns is not None, (
                f"{path.name} inserts into payments positionally, with no column list — "
                "this vector grades columns by name and cannot see what that writes"
            )
            assert "cvv" not in columns.lower(), (
                f"{path.name} still seeds a CVV: INSERT INTO payments ({columns.strip()})"
            )
    assert seen, "no db/init file inserts payments — update this vector"


# --- the migration -------------------------------------------------------------------


def test_the_purge_migration_exists():
    assert MIGRATION.exists(), (
        "db/migrations/0020_payments_drop_cvv.sql is missing — an existing volume keeps "
        "the column and every value in it"
    )


def test_the_migration_nulls_the_values_before_it_drops_the_column():
    sql = _migration_statements()
    null_at = sql.find("set cvv = null")
    drop_at = sql.find("drop column")
    assert null_at != -1, "migration never NULLs the stored values"
    assert drop_at != -1, "migration never drops the column"
    assert null_at < drop_at, (
        "the UPDATE must precede the DROP: dropping first leaves the values in the live "
        "row versions with no column to clear them through"
    )


def test_the_migration_rewrites_the_table():
    sql = _migration_statements()
    assert "vacuum full" in sql or "pg_repack" in sql, (
        "no table rewrite: DROP COLUMN only marks the attribute dropped and the UPDATE "
        "leaves dead tuples, so every CVV stays readable in the data files"
    )


# --- the code ------------------------------------------------------------------------


def test_the_claim_insert_names_no_cvv_column():
    assert "cvv" not in payments._CLAIM_SQL.lower(), payments._CLAIM_SQL


def test_the_request_model_has_no_cvv_field():
    assert "cvv" not in PaymentIn.model_fields


def test_a_stale_caller_sending_cvv_is_accepted_and_the_value_is_dropped():
    """Ignored, not rejected: an integration that still sends the field keeps working,
    and the value binds to nothing, is never logged as a bound field, and is never
    stored. SAD reaches a process boundary and no store."""
    try:
        body = PaymentIn(loan_id=4471, amount=250.0, method="card", cvv="123")
    except ValidationError as exc:  # pragma: no cover - only on a scope change
        pytest.fail(f"a stale caller was refused rather than ignored: {exc}")
    assert not hasattr(body, "cvv")
    assert "cvv" not in body.model_dump()


def test_charge_takes_no_cvv_parameter():
    assert "cvv" not in inspect.signature(payments.charge).parameters


def test_claim_or_branch_takes_no_cvv_parameter():
    assert "cvv" not in inspect.signature(payments.claim_or_branch).parameters


def test_the_charge_log_allowlist_has_no_cvv_key():
    logged = payments._redacted_charge_req("4111111111111111", None, 250.0, 4471)
    assert "cvv" not in logged


# --- readiness -----------------------------------------------------------------------


class _Cursor:
    """Minimal cursor: answers the rung's one lookup with whatever row is configured."""

    def __init__(self, row):
        self._row = row
        self.executed = []

    def execute(self, sql, args=None):
        self.executed.append((sql, args))

    def fetchone(self):
        return self._row


def test_readiness_refuses_a_volume_that_still_has_the_column():
    ok, reason = _no_stored_sad_ready(_Cursor(("cvv",)))
    assert ok is False
    assert reason == "schema_not_ready:payments.cvv_present"


def test_readiness_passes_once_the_column_is_gone():
    assert _no_stored_sad_ready(_Cursor(None)) == (True, None)


def test_the_readiness_lookup_resolves_the_table_the_charge_path_writes():
    cur = _Cursor(None)
    _no_stored_sad_ready(cur)
    sql, _ = cur.executed[0]
    assert "to_regclass('payments')" in sql, (
        "the rung must grade the payments table this connection actually resolves, by "
        "search_path — an information_schema lookup spans every schema the connection can "
        "see, and qualifying it by current_schema() grades a table the charge path's "
        "INSERT may not even be writing to, clearing the volume either way"
    )


# --- the money path itself -----------------------------------------------------------
#
# The rung above is only reached from /health, and /health is advisory: nothing consults
# it before a capture. The legacy cvv column is NULLABLE, so a charge that never names it
# inserts fine on a volume that skipped migration 0020 -- the service reports unhealthy
# while every capture keeps landing rows in a table still retaining every CVV ever
# stored. So the charge handler has to refuse for itself, at the route, before capture.

_ROUTE_IDEM_KEY = "22222222-2222-4222-8222-222222222222"


def _unmigrated(monkeypatch):
    """A volume that still carries payments.cvv, as the readiness probe reports it."""
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "proc_test")
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(
        config,
        "database_reachable",
        lambda *a, **k: (False, "schema_not_ready:payments.cvv_present"),
    )


def test_the_charge_route_refuses_a_volume_that_still_has_the_column(monkeypatch):
    _unmigrated(monkeypatch)

    def _boom(*a, **k):
        raise AssertionError(
            "a capture ran against a volume that still stores CVV — the guard is not "
            "on the money path, only on /health"
        )

    monkeypatch.setattr(payments, "charge", _boom)

    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 250.0, "method": "card"},
        headers={"Idempotency-Key": _ROUTE_IDEM_KEY, "X-User-Role": "csr"},
    )

    assert resp.status_code == 503, resp.text
    assert "payments.cvv_present" in resp.text, (
        "the refusal must name the rung that refused, or an operator cannot tell an "
        "unmigrated volume from an unreachable database"
    )


def test_the_charge_route_serves_once_the_volume_is_migrated(monkeypatch):
    """The other half: the guard refuses THIS condition, not every charge."""
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "proc_test")
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "sekret")
    monkeypatch.setattr(config, "database_reachable", lambda *a, **k: (True, None))
    monkeypatch.setattr(
        payments,
        "charge",
        lambda *a, **k: {
            "payment_id": 1,
            "loan_id": 1,
            "status": "captured",
            "applied_amount": 250.0,
        },
    )

    resp = TestClient(app).post(
        "/payments",
        json={"loan_id": 1, "amount": 250.0, "method": "card"},
        headers={"Idempotency-Key": _ROUTE_IDEM_KEY, "X-User-Role": "csr"},
    )

    assert resp.status_code == 200, resp.text


# --- the backstop --------------------------------------------------------------------


def test_the_redactor_still_masks_a_cvv_in_a_log_line(caplog):
    """The column is gone; the field can still arrive in a body and be logged."""
    log = get_logger("no-sad-vector")
    with caplog.at_level(logging.INFO):
        log.info("stale caller body=%s", json.dumps({"cvv": "123"}))
    assert "123" not in caplog.text
