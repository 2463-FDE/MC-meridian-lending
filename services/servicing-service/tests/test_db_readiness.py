"""DB readiness regression.

An unset, passwordless, placeholder, or stale/drifted DATABASE_URL must read as
misconfigured so /health can report unhealthy instead of connecting
unauthenticated or failing auth at first query. Covers the passwordless DSN
(meridian:@postgres) the secret purge left behind and the shipped placeholder.
"""

import threading
import time

import pytest

from app import config


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    # database_reachable caches its result; reset around every test so the probe
    # stubs below are observed fresh and cases don't leak into each other.
    config.reset_database_probe_cache()
    yield
    config.reset_database_probe_cache()


def test_unset_database_url_is_misconfigured(monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(config, "DATABASE_URL", "")
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_passwordless_database_url_is_misconfigured(monkeypatch):
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:@postgres:5432/meridian"
    )
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_placeholder_database_url_is_misconfigured(monkeypatch):
    # The .env.example placeholder has a non-empty password string but is not a
    # real credential — it must not read as healthy.
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql://meridian:REPLACE_WITH_POSTGRES_PASSWORD@postgres:5432/meridian",
    )
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_stale_password_rejected_against_postgres_password(monkeypatch):
    # DSN password drifted from POSTGRES_PASSWORD (source of truth) -> caught
    # without a DB round trip.
    monkeypatch.setenv("POSTGRES_PASSWORD", "the_real_pw")
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql://meridian:stale_old_pw@postgres:5432/meridian",
    )
    assert config.database_url_configured() is False
    assert "DATABASE_URL" in config.missing_required_secrets()


def test_password_matching_postgres_password_is_ok(monkeypatch):
    monkeypatch.setenv("POSTGRES_PASSWORD", "the_real_pw")
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql://meridian:the_real_pw@postgres:5432/meridian",
    )
    assert config.database_url_configured() is True
    assert "DATABASE_URL" not in config.missing_required_secrets()


def test_password_bearing_database_url_is_ok(monkeypatch):
    # No POSTGRES_PASSWORD reference (e.g. external managed DB) -> a real,
    # non-placeholder password is accepted.
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    assert config.database_url_configured() is True
    assert "DATABASE_URL" not in config.missing_required_secrets()


def test_url_encoded_reserved_char_password_is_ok(monkeypatch):
    # A password with reserved URL chars must be percent-encoded in the DSN
    # (p@ss/word:1 -> p%40ss%2Fword%3A1); the gate must decode before comparing
    # to POSTGRES_PASSWORD, else a valid password is falsely flagged stale.
    monkeypatch.setenv("POSTGRES_PASSWORD", "p@ss/word:1")
    monkeypatch.setattr(
        config,
        "DATABASE_URL",
        "postgresql://meridian:p%40ss%2Fword%3A1@postgres:5432/meridian",
    )
    assert config.database_url_configured() is True
    assert "DATABASE_URL" not in config.missing_required_secrets()


# --- Live connectivity probe (database_reachable) --------------------------
# The config gate above cannot prove a password authenticates. database_reachable
# opens a bounded connection and runs SELECT 1; these tests stub psycopg2.connect
# so no real database is required.


# The D19 readiness rung (config._payments_idempotency_ready) asks the catalog real
# questions: the data_type of each migration-0018 column, and for each partial unique
# index its indisunique / table / covered column / predicate. A cursor that answered
# every query with a blanket (1,) would make the probe pass without any of those
# questions being answered -- a fake that reports success for what it never checked.
# So this one dispatches on the SQL and models a database that HAS 0018 applied.
_PAYMENTS_COLUMN_TYPES = {
    "idempotency_key": "text",
    "idempotency_expires_at": "timestamp with time zone",
    "request_fingerprint": "text",
    "status": "text",
    "processor_idempotency_key": "text",
    "processor_ref": "text",
    "amount_minor": "bigint",
    "updated_at": "timestamp with time zone",
}
_PAYMENTS_INDEX_COLUMNS = {
    "payments_idempotency_key_uniq": "idempotency_key",
    "payments_processor_idempotency_key_uniq": "processor_idempotency_key",
}


class _FakeCursor:
    """Cursor over a correctly-migrated database.

    `overrides` maps a column or index name to the row the catalog should return
    instead, which is how the not-ready tests below drive one failure mode at a time.
    """

    def __init__(self, overrides=None):
        self.overrides = overrides or {}
        self._result = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._result = self._answer(sql, params)

    def _answer(self, sql, params):
        name = params[0] if params else None
        if "pg_index" in sql:
            if name in self.overrides:
                return self.overrides[name]
            column = _PAYMENTS_INDEX_COLUMNS[name]
            return (True, "payments", f"({column} IS NOT NULL)", column)
        if "is_nullable" in sql:
            # status NOT NULL DEFAULT 'captured' contract query -- unparameterized
            # (column_name is a literal in the SQL), so it is not keyed by `name`.
            return self.overrides.get("status_contract", ("NO", "'captured'::text"))
        if "information_schema.columns" in sql and "'payments'" in sql:
            if name in self.overrides:
                return self.overrides[name]
            return (_PAYMENTS_COLUMN_TYPES[name],)
        # loans.note_rate rung (servicing only) and the plain SELECT 1.
        return self.overrides.get("loans.note_rate", (1,))

    def fetchone(self):
        return self._result


class _FakeConn:
    def __init__(self, overrides=None):
        self.closed_flag = False
        self.overrides = overrides

    def cursor(self):
        return _FakeCursor(self.overrides)

    def close(self):
        self.closed_flag = True


def test_probe_ok_when_connection_succeeds(monkeypatch):
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(config.psycopg2, "connect", lambda *a, **k: _FakeConn())
    ok, err = config.database_reachable()
    assert ok is True
    assert err is None


def test_probe_fails_on_wrong_password_without_postgres_password(monkeypatch):
    # The documented residual the config gate cannot catch: a real, non-placeholder
    # DSN password with no POSTGRES_PASSWORD to compare against. database_url_configured
    # accepts it; only the live probe detects that it does not authenticate.
    monkeypatch.delenv("POSTGRES_PASSWORD", raising=False)
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:wrong_pw@postgres:5432/meridian"
    )
    assert config.database_url_configured() is True  # config gate cannot tell

    def _auth_fail(*a, **k):
        raise config.psycopg2.OperationalError("password authentication failed")

    monkeypatch.setattr(config.psycopg2, "connect", _auth_fail)
    ok, err = config.database_reachable()
    assert ok is False
    assert err == "OperationalError"  # class name only — no DSN/password leak


def test_probe_false_when_database_url_unset(monkeypatch):
    monkeypatch.setattr(config, "DATABASE_URL", "")
    ok, err = config.database_reachable()
    assert ok is False


def test_probe_passes_bounded_timeouts_and_closes(monkeypatch):
    captured = {}
    conn = _FakeConn()

    def _capture(dsn, **kwargs):
        captured.update(kwargs)
        return conn

    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(config.psycopg2, "connect", _capture)
    config.database_reachable()
    assert captured["connect_timeout"] >= 1
    assert "statement_timeout" in captured["options"]
    assert conn.closed_flag is True  # connection is always closed


def test_probe_result_is_cached_within_ttl(monkeypatch):
    # /health must not open a Postgres connection per request. Two calls within the
    # TTL must reuse one connection — the DoS-amplifier fix.
    calls = {"n": 0}

    def _count(*a, **k):
        calls["n"] += 1
        return _FakeConn()

    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(config.psycopg2, "connect", _count)
    config.database_reachable()
    config.database_reachable()
    assert calls["n"] == 1  # second call served from cache, no new connection


def test_probe_single_flight_under_concurrent_misses(monkeypatch):
    # N threads hit a cold cache simultaneously; single-flight must collapse them
    # to ONE psycopg2.connect, not one connection per request (the /health-flood fix).
    calls = {"n": 0}
    count_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def _slow_connect(*a, **k):
        with count_lock:
            calls["n"] += 1
        time.sleep(0.05)  # hold the probe so all threads pile onto the miss path
        return _FakeConn()

    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(config.psycopg2, "connect", _slow_connect)

    results = []

    def worker():
        barrier.wait()  # release all threads at once -> simultaneous cold-cache miss
        results.append(config.database_reachable())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert calls["n"] == 1  # exactly one probe despite 8 concurrent misses
    assert results == [(True, None)] * 8


# --- Schema rung: loans.note_rate --------------------------------------------
# The Loan ORM entity maps loans.note_rate (migration 0014), so every servicing loan
# read (list, detail, schedule) loads it. Migrations lag the init DDL, so a volume that
# has the loans table but not this column would 500 EVERY loan read while /health
# otherwise read fine. The probe asserts the column with the right type; absent -> unready.


class _SchemaAwareCursor(_FakeCursor):
    """SELECT 1 is truthy; the loans.note_rate probe answers by a flag.

    Everything else -- the migration-0018 payments columns and both partial unique
    indexes -- is delegated to _FakeCursor, which models a correctly-migrated volume.
    Answering those with a blanket (1,) here would make this fixture report ready over
    a schema the D19 rung would in fact refuse, so the note_rate cases would pass for
    the wrong reason.
    """

    def __init__(self, note_rate_present):
        super().__init__()
        self._note_rate_present = note_rate_present

    def _answer(self, sql, params):
        if "information_schema.columns" in sql and "'loans'" in sql:
            return (1,) if self._note_rate_present else None
        return super()._answer(sql, params)


class _SchemaAwareConn:
    def __init__(self, note_rate_present):
        self._present = note_rate_present
        self.closed_flag = False

    def cursor(self):
        return _SchemaAwareCursor(self._present)

    def close(self):
        self.closed_flag = True


def test_probe_unready_when_note_rate_column_absent(monkeypatch):
    # Regression: a volume predating migration 0014 has the loans table but not
    # loans.note_rate. The connection opens and SELECT 1 succeeds, but the schema rung
    # must report unready — otherwise /health reads ok while every loan read 500s on the
    # ORM's note_rate SELECT.
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(
        config.psycopg2,
        "connect",
        lambda *a, **k: _SchemaAwareConn(note_rate_present=False),
    )
    ok, err = config.database_reachable()
    assert ok is False
    assert err == "schema_not_ready:loans.note_rate"


def test_probe_ready_when_note_rate_column_present(monkeypatch):
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(
        config.psycopg2,
        "connect",
        lambda *a, **k: _SchemaAwareConn(note_rate_present=True),
    )
    ok, err = config.database_reachable()
    assert ok is True
    assert err is None


# --- Processor-key readiness + fail-closed capture -------------------------
# The direct /payments path inserts a payment AND mutates the balance. After the
# secret purge PROCESSOR_API_KEY has no committed fallback, so without it the
# service must read unhealthy AND /payments must fail closed.


def test_missing_processor_key_flags_readiness(monkeypatch):
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "")
    assert "PROCESSOR_API_KEY" in config.missing_required_secrets()


def test_present_processor_key_not_flagged(monkeypatch):
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "proc_test")
    assert "PROCESSOR_API_KEY" not in config.missing_required_secrets()


def test_payments_503_without_processor_key(monkeypatch):
    # Call the route handler directly (no TestClient/httpx dependency): the guard
    # fires before charge(), so no DB / balance mutation happens.
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "")
    from fastapi import HTTPException
    from app.main import PaymentIn, post_payment

    with pytest.raises(HTTPException) as exc_info:
        post_payment(PaymentIn(loan_id=1, amount=100.0), x_user_role="csr")
    assert exc_info.value.status_code == 503


def test_payments_allowed_with_processor_key(monkeypatch):
    monkeypatch.setattr(config, "PROCESSOR_API_KEY", "proc_test")
    from app import payments

    monkeypatch.setattr(
        payments,
        "charge",
        lambda *a, **k: {"loan_id": 1, "amount": 100.0, "balance": 900.0},
    )
    from app.main import PaymentIn, post_payment

    out = post_payment(PaymentIn(loan_id=1, amount=100.0), x_user_role="csr")
    assert out["balance"] == 900.0


# DUPLICATE_SUSPECT_WINDOW_SECONDS (D2(e)) — without it, reconciliation.reconcile()
# aborts on the very first call. Unguarded here, a deploy would pass /health and only
# fail when /reconciliation/peek is invoked: the hidden-control-failure class this
# whole readiness surface exists to remove.


@pytest.mark.parametrize("bad_value", ["", "abc", "0", "-5", "99999999999999999999"])
def test_missing_or_invalid_duplicate_window_flags_readiness(monkeypatch, bad_value):
    monkeypatch.setattr(config, "DUPLICATE_SUSPECT_WINDOW_SECONDS", bad_value)
    assert "DUPLICATE_SUSPECT_WINDOW_SECONDS" in config.missing_required_secrets()


def test_valid_duplicate_window_not_flagged(monkeypatch):
    monkeypatch.setattr(config, "DUPLICATE_SUSPECT_WINDOW_SECONDS", "120")
    assert "DUPLICATE_SUSPECT_WINDOW_SECONDS" not in config.missing_required_secrets()


def test_duplicate_window_ceiling_matches_reconciliations_own_abort(monkeypatch):
    """config.duplicate_suspect_window_configured() and reconciliation.py's
    _duplicate_suspect_window_seconds() share config.MAX_DUPLICATE_SUSPECT_WINDOW_
    SECONDS rather than each declaring their own copy — this pins that a value one
    past the ceiling is flagged by both, so they cannot silently drift apart."""
    from app import reconciliation

    over_ceiling = str(config.MAX_DUPLICATE_SUSPECT_WINDOW_SECONDS + 1)
    monkeypatch.setattr(config, "DUPLICATE_SUSPECT_WINDOW_SECONDS", over_ceiling)
    monkeypatch.setattr(
        reconciliation, "DUPLICATE_SUSPECT_WINDOW_SECONDS", over_ceiling
    )

    assert "DUPLICATE_SUSPECT_WINDOW_SECONDS" in config.missing_required_secrets()
    with pytest.raises(reconciliation.ReconciliationAbort):
        reconciliation._duplicate_suspect_window_seconds()


# --- D19 schema rung (migration 0018) ---------------------------------------------
#
# The charge path claims the idempotency key with INSERT ... ON CONFLICT against a
# PARTIAL unique index. On a volume missing 0018 that insert raises "no unique or
# exclusion constraint matching the ON CONFLICT specification" -- the double-charge
# control failing on first use, while /health otherwise reads fine. Migrations here are
# hand-applied and lag the init DDL, so the rung has to name the gap at readiness.
#
# Each case below breaks exactly ONE catalog fact. They exist because CREATE UNIQUE
# INDEX IF NOT EXISTS and ADD COLUMN IF NOT EXISTS both match on NAME alone: an index
# that is non-unique, on the wrong column, or missing its predicate, and a column of the
# wrong type, all survive those statements and would report ready under a rung that
# checked names.


def _probe_with(monkeypatch, overrides):
    monkeypatch.setattr(
        config, "DATABASE_URL", "postgresql://meridian:s3cret@postgres:5432/meridian"
    )
    monkeypatch.setattr(
        config.psycopg2, "connect", lambda *a, **k: _FakeConn(overrides)
    )
    return config.database_reachable()


def test_rung_not_ready_when_an_idempotency_column_is_absent(monkeypatch):
    ok, err = _probe_with(monkeypatch, {"idempotency_key": None})
    assert ok is False
    assert err == "schema_not_ready:payments.idempotency_key"


def test_rung_not_ready_when_amount_minor_has_the_wrong_type(monkeypatch):
    """ADD COLUMN IF NOT EXISTS swallows a same-named column of any type.

    A TEXT stand-in for BIGINT reports ready under a name-only check and then hands
    every reader a string where minor units are expected.
    """
    ok, err = _probe_with(monkeypatch, {"amount_minor": ("text",)})
    assert ok is False
    assert err == "schema_not_ready:payments.amount_minor"


def test_rung_not_ready_when_processor_ref_is_absent(monkeypatch):
    """The rung must prove ALL of migration 0018's columns, not a subset -- a volume
    missing processor_ref (present in the migration and init DDL) previously read
    ready anyway."""
    ok, err = _probe_with(monkeypatch, {"processor_ref": None})
    assert ok is False
    assert err == "schema_not_ready:payments.processor_ref"


def test_rung_not_ready_when_updated_at_has_the_wrong_type(monkeypatch):
    ok, err = _probe_with(monkeypatch, {"updated_at": ("text",)})
    assert ok is False
    assert err == "schema_not_ready:payments.updated_at"


def test_rung_not_ready_when_status_is_nullable(monkeypatch):
    """data_type alone does not prove the NOT NULL DEFAULT contract: ADD COLUMN IF NOT
    EXISTS no-ops on an existing nullable status column of the right type."""
    ok, err = _probe_with(monkeypatch, {"status_contract": ("YES", "'captured'::text")})
    assert ok is False
    assert err == "schema_not_ready:payments.status"


def test_rung_not_ready_when_status_default_is_not_captured(monkeypatch):
    ok, err = _probe_with(monkeypatch, {"status_contract": ("NO", "'pending'::text")})
    assert ok is False
    assert err == "schema_not_ready:payments.status"


def test_rung_not_ready_when_status_has_no_default(monkeypatch):
    ok, err = _probe_with(monkeypatch, {"status_contract": ("NO", None)})
    assert ok is False
    assert err == "schema_not_ready:payments.status"


def test_rung_not_ready_when_status_default_merely_contains_captured(monkeypatch):
    """An unanchored substring match ('captured' in default) would pass 'recaptured' or
    'uncaptured' -- a wrong default that happens to share the substring. The rung must
    compare the full rendered expression, not just contain the right word."""
    ok, err = _probe_with(
        monkeypatch, {"status_contract": ("NO", "'recaptured'::text")}
    )
    assert ok is False
    assert err == "schema_not_ready:payments.status"


def test_rung_not_ready_when_the_index_predicate_is_on_the_wrong_column(monkeypatch):
    """A predicate containing "IS NOT NULL" anywhere is not enough -- it must be on the
    column this index is supposed to be partial on, or a drifted index (e.g. a stray
    loan_id predicate on what is otherwise the idempotency_key index) reads as ready."""
    ok, err = _probe_with(
        monkeypatch,
        {
            "payments_idempotency_key_uniq": (
                True,
                "payments",
                "(loan_id IS NOT NULL)",
                "idempotency_key",
            )
        },
    )
    assert ok is False
    assert err == "schema_not_ready:payments_idempotency_key_uniq"


def test_rung_not_ready_when_indexed_column_is_null_and_another_is_not_null(
    monkeypatch,
):
    """A loose ".*IS NOT NULL" gap would pass a compound predicate where the INDEXED
    column is checked IS NULL and an unrelated column is IS NOT NULL later in the same
    expression -- the column name and "IS NOT NULL" must be adjacent, not just both
    present somewhere in the predicate."""
    ok, err = _probe_with(
        monkeypatch,
        {
            "payments_idempotency_key_uniq": (
                True,
                "payments",
                "(idempotency_key IS NULL) OR (processor_ref IS NOT NULL)",
                "idempotency_key",
            )
        },
    )
    assert ok is False
    assert err == "schema_not_ready:payments_idempotency_key_uniq"


def test_rung_not_ready_when_the_predicate_is_narrower_than_the_arbiter(monkeypatch):
    """A compound predicate like "col IS NOT NULL AND amount_minor > 0" contains the
    column immediately followed by IS NOT NULL, so a substring/regex check -- even
    column-bound -- would pass it. It is a DIFFERENT, smaller index than the arbiter
    the shipped ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL names,
    so Postgres cannot infer it for every claimed row. Only an exact match on the full
    predicate is correct."""
    ok, err = _probe_with(
        monkeypatch,
        {
            "payments_idempotency_key_uniq": (
                True,
                "payments",
                "(idempotency_key IS NOT NULL AND amount_minor > 0)",
                "idempotency_key",
            )
        },
    )
    assert ok is False
    assert err == "schema_not_ready:payments_idempotency_key_uniq"


def test_rung_not_ready_when_the_index_is_absent(monkeypatch):
    ok, err = _probe_with(monkeypatch, {"payments_idempotency_key_uniq": None})
    assert ok is False
    assert err == "schema_not_ready:payments_idempotency_key_uniq"


def test_rung_not_ready_when_the_index_exists_but_is_not_unique(monkeypatch):
    """A non-unique index of the right name cannot arbitrate ON CONFLICT."""
    ok, err = _probe_with(
        monkeypatch,
        {
            "payments_idempotency_key_uniq": (
                False,
                "payments",
                "(idempotency_key IS NOT NULL)",
                "idempotency_key",
            )
        },
    )
    assert ok is False
    assert err == "schema_not_ready:payments_idempotency_key_uniq"


def test_rung_not_ready_when_the_index_is_not_partial(monkeypatch):
    """No predicate means the shipped ON CONFLICT ... WHERE cannot infer the arbiter --

    Postgres cannot pick an arbiter for a partial-predicate ON CONFLICT clause without a
    matching partial index, regardless of whether the underlying index would otherwise
    behave correctly (it never collides pre-0018 NULL rows either way; Postgres treats
    every NULL as distinct in a unique index).
    """
    ok, err = _probe_with(
        monkeypatch,
        {
            "payments_idempotency_key_uniq": (
                True,
                "payments",
                None,
                "idempotency_key",
            )
        },
    )
    assert ok is False
    assert err == "schema_not_ready:payments_idempotency_key_uniq"


def test_rung_not_ready_when_the_index_covers_the_wrong_column(monkeypatch):
    ok, err = _probe_with(
        monkeypatch,
        {
            "payments_processor_idempotency_key_uniq": (
                True,
                "payments",
                "(loan_id IS NOT NULL)",
                "loan_id",
            )
        },
    )
    assert ok is False
    assert err == "schema_not_ready:payments_processor_idempotency_key_uniq"


def test_rung_ready_on_a_correctly_migrated_volume(monkeypatch):
    ok, err = _probe_with(monkeypatch, {})
    assert ok is True
    assert err is None
