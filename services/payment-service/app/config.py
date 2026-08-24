import os
import threading
import time
from urllib.parse import unquote, urlparse

import psycopg2

# No committed default: a passwordless fallback DSN (meridian:@postgres) would
# let a deploy that omits DATABASE_URL connect unauthenticated and look healthy.
# Unset/passwordless is reported unhealthy via missing_required_secrets().
DATABASE_URL = os.getenv("DATABASE_URL", "")


def database_url_configured() -> bool:
    """True only when DATABASE_URL is set with a real, consistent password.

    Password auth is how this stack reaches Postgres (compose sets
    POSTGRES_PASSWORD via ${VAR:?}; .env.example embeds it in the DSN). A
    non-empty password is necessary but NOT sufficient: the template ships a
    REPLACE_WITH_POSTGRES_PASSWORD placeholder, and an operator can set
    POSTGRES_PASSWORD but leave the DSN on that placeholder or a stale value —
    /health would read healthy while the first real query fails auth. So this
    also rejects known placeholder/stub tokens and, when POSTGRES_PASSWORD is
    present as the source of truth, requires the DSN password to match it,
    catching placeholder/stale drift without a DB round trip.

    Residual (documented): this proves the password is real and consistent, not
    that it authenticates. A wrong password with no POSTGRES_PASSWORD to compare
    (e.g. an external managed DB whose secret lives only in the DSN) is caught by
    database_reachable(), the bounded live probe /health runs after this gate.
    Passwordless auth (IAM/peer/PGPASSWORD)
    must revisit this gate — here, passwordless means misconfigured.
    """
    if not DATABASE_URL:
        return False
    try:
        password = urlparse(DATABASE_URL).password
    except ValueError:
        return False
    if not password:
        return False
    # urlparse returns the percent-ENCODED password; decode it so a reserved-char
    # password (p@ss -> p%40ss in the DSN) is not falsely flagged as a placeholder
    # or as drifted from raw POSTGRES_PASSWORD.
    password = unquote(password)
    # Known placeholder passwords are never valid. The previously-committed
    # credential is intentionally NOT listed here — embedding that literal would
    # re-commit the leaked secret in every clone/image and defeat the purge. A
    # stale/rotated DSN is caught by the POSTGRES_PASSWORD consistency check below.
    if password.lower() in {
        "replace_with_postgres_password",
        "changeme",
        "change_me",
        "password",
        "postgres",
    }:
        return False
    # When POSTGRES_PASSWORD is the source of truth (compose ${VAR:?}), the DSN
    # password must match it — catches a stale/placeholder DSN without a DB call.
    expected = os.getenv("POSTGRES_PASSWORD")
    if expected and password != expected:
        return False
    return True


# Short-TTL cache of the last probe, keyed on the DSN. /health is unauthenticated
# and hit by load balancers (and anyone on the published port); without this each
# request would open a new, unpooled Postgres connection, so a flood could exhaust
# max_connections or the sync threadpool. Collapsing bursts to one probe per TTL
# removes that amplifier. Cost: /health can lag a DB up/down transition by up to
# the TTL — acceptable for readiness (the healthcheck interval is longer). Stored
# as a single tuple so a concurrent read never sees torn state under the GIL.
_PROBE_TTL_SECONDS = 5.0
_probe_state = (None, 0.0, (False, None))  # (dsn, monotonic_at, result)
# Single-flight: only one thread probes per DSN/TTL window; concurrent misses wait
# on this and reuse the fresh result instead of each opening its own connection.
_probe_lock = threading.Lock()


def reset_database_probe_cache() -> None:
    """Drop the cached probe result (forces the next call to reconnect)."""
    global _probe_state
    _probe_state = (None, 0.0, (False, None))


def database_reachable(timeout: float = 2.0) -> tuple[bool, str | None]:
    """Bounded live probe (TTL-cached): open a Postgres connection, run SELECT 1.

    database_url_configured() only proves the DSN password is non-placeholder and
    (when POSTGRES_PASSWORD is set) matches it — it does NOT prove the password
    authenticates. This closes that documented residual: a wrong password with no
    POSTGRES_PASSWORD to compare against (e.g. an external managed DB whose secret
    lives only in the DSN) is caught only by actually connecting. connect_timeout
    and a server-side statement_timeout bound the probe so /health cannot hang; the
    result is cached for _PROBE_TTL_SECONDS so a flood of /health calls cannot open
    a Postgres connection per request.

    Returns (ok, error); error is the exception class name only — never the DSN or
    its password — so /health cannot leak credentials.
    """
    global _probe_state
    dsn, at, result = _probe_state
    if dsn == DATABASE_URL and (time.monotonic() - at) < _PROBE_TTL_SECONDS:
        return result
    # Cold cache or expired TTL: single-flight so a burst of concurrent misses
    # (e.g. an unauthenticated /health flood) performs ONE probe, not one Postgres
    # connection per request. The check above stays lock-free for the warm path;
    # only a miss contends on the lock, and the re-check inside lets every caller
    # after the winner reuse the fresh result.
    with _probe_lock:
        dsn, at, result = _probe_state
        if dsn == DATABASE_URL and (time.monotonic() - at) < _PROBE_TTL_SECONDS:
            return result
        result = _run_database_probe(timeout)
        _probe_state = (DATABASE_URL, time.monotonic(), result)
        return result


# Schema rung for D19 (migration 0018). This service's charge path claims the
# idempotency key with an INSERT ... ON CONFLICT against a PARTIAL unique index, so a
# volume that has the payments table but not migration 0018 would raise "no unique or
# exclusion constraint matching the ON CONFLICT specification" on the first charge —
# the double-charge control failing at the moment it is first needed, while /health
# otherwise read fine. Migrations here are hand-applied and lag the init DDL, so the
# gap has to be named at readiness rather than discovered by a borrower.
#
# The NAME of an index proves nothing: CREATE UNIQUE INDEX IF NOT EXISTS matches on
# name alone, so a same-named index that is non-unique, on the wrong column, or missing
# the partial predicate would leave the control disabled while reporting ready. Assert
# the definition — indisunique, the table, the covered column, and the predicate — and
# assert the column TYPE too, since ADD COLUMN IF NOT EXISTS swallows a same-named
# column of any type and a TEXT stand-in for BIGINT hands every reader a string.
#
# Kept byte-identical to servicing-service's copy: both services write this table
# (debt D23) and both claim the key, so a rung that drifts between them would let one
# service report ready over a schema the other refuses.
_IDEMPOTENCY_COLUMNS = (
    ("idempotency_key", "text"),
    ("idempotency_expires_at", "timestamp with time zone"),
    ("request_fingerprint", "text"),
    ("status", "text"),
    ("processor_idempotency_key", "text"),
    ("processor_ref", "text"),
    ("amount_minor", "bigint"),
    ("updated_at", "timestamp with time zone"),
)
_IDEMPOTENCY_INDEXES = (
    ("payments_idempotency_key_uniq", "idempotency_key"),
    ("payments_processor_idempotency_key_uniq", "processor_idempotency_key"),
)


def _payments_idempotency_ready(cur) -> tuple[bool, str | None]:
    """Assert migration 0018 is actually applied, by definition and not by name."""
    for column, want_type in _IDEMPOTENCY_COLUMNS:
        cur.execute(
            # table_schema is not optional: information_schema.columns spans every
            # schema, so an unqualified lookup can validate a different `payments`.
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'payments' AND column_name = %s",
            (column,),
        )
        row = cur.fetchone()
        if row is None or row[0] != want_type:
            return False, f"schema_not_ready:payments.{column}"
    # data_type alone does not prove the NOT NULL DEFAULT 'captured' contract migration
    # 0018 sets: ADD COLUMN IF NOT EXISTS no-ops on an existing status column, including
    # its NOT NULL DEFAULT clause, so a volume where status was already TEXT but nullable
    # or undefaulted would pass the loop above and then take a NULL/wrong-status row from
    # either insert path, which omits status.
    cur.execute(
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = current_schema() "
        "AND table_name = 'payments' AND column_name = 'status'"
    )
    row = cur.fetchone()
    if row is None:
        return False, "schema_not_ready:payments.status"
    status_nullable, status_default = row
    # column_default is the quoted+cast expression ('captured'::text). An unanchored
    # substring match ('captured' in status_default) would also pass a wrong default
    # like 'recaptured' or 'uncaptured' -- exactly the class of loose match this rung
    # exists to refuse. Compare the FULL expression, since status's data_type is
    # already asserted 'text' above and so is its rendering.
    if status_nullable != "NO" or status_default != "'captured'::text":
        return False, "schema_not_ready:payments.status"
    for index, column in _IDEMPOTENCY_INDEXES:
        cur.execute(
            "SELECT x.indisunique, c.relname, "
            "       pg_get_expr(x.indpred, x.indrelid), "
            "       (SELECT string_agg(a.attname, ',' ORDER BY k.ord) "
            "          FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord) "
            "          JOIN pg_attribute a "
            "            ON a.attrelid = x.indrelid AND a.attnum = k.attnum) "
            "  FROM pg_index x "
            "  JOIN pg_class i ON i.oid = x.indexrelid "
            "  JOIN pg_namespace n ON n.oid = i.relnamespace "
            "  JOIN pg_class c ON c.oid = x.indrelid "
            # relname is unique per SCHEMA, not per database: an index of the same
            # name in another schema would otherwise answer this probe.
            " WHERE i.relname = %s AND n.nspname = current_schema()",
            (index,),
        )
        row = cur.fetchone()
        if row is None:
            return False, f"schema_not_ready:{index}"
        is_unique, table, predicate, columns = row
        if not is_unique or table != "payments" or columns != column:
            return False, f"schema_not_ready:{index}"
        # The predicate is what makes the arbiter inferable by the shipped
        # ON CONFLICT ... WHERE clause; a non-partial index of the same name would
        # make that insert raise. Compare EXACTLY to pg_get_expr's stable
        # "(col IS NOT NULL)" rendering, same as migration 0018's own assertion -- a
        # regex/substring match, even column-bound, would also pass a narrower,
        # compound predicate like "col IS NOT NULL AND amount_minor > 0", which is a
        # DIFFERENT (smaller) index than the arbiter the shipped ON CONFLICT names and
        # cannot be inferred for every claimed row.
        if predicate != f"({column} IS NOT NULL)":
            return False, f"schema_not_ready:{index}"
    return True, None


# Schema rung for D3 (migration 0019). Both services touch payment_applications: servicing
# writes it inside the atomic apply, and payment-service's _RETIRE_SQL reads it to decide
# whether a `captured` key may be retired. A volume without migration 0019 would fail the
# apply with "relation payment_applications does not exist" — a 500 on the money path
# instead of a named gap — and would refuse every retirement, so both need the rung and
# both must agree on what ready means.
#
# The NAME of the table proves nothing: CREATE TABLE IF NOT EXISTS matches on name alone,
# so a same-named table with a nullable amount_minor or no UNIQUE on payment_id would
# leave the replay guard disabled while reporting ready. Assert the column types, the
# NOT NULLs, and the unique index's definition — the same assertions migration 0019 makes.
#
# Kept byte-identical to the other service's copy, same as _payments_idempotency_ready
# above: a rung that drifts between them lets one service report ready over a schema the
# other refuses.
_APPLICATION_COLUMNS = (
    ("id", "integer"),
    ("loan_id", "integer"),
    ("payment_id", "integer"),
    ("amount_minor", "bigint"),
    ("created_at", "timestamp with time zone"),
)


def _payment_applications_ready(cur) -> tuple[bool, str | None]:
    """Assert migration 0019 is actually applied, by definition and not by name."""
    for column, want_type in _APPLICATION_COLUMNS:
        cur.execute(
            # table_schema is not optional: information_schema.columns spans every
            # schema, so an unqualified lookup can validate a different table.
            "SELECT data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'payment_applications' AND column_name = %s",
            (column,),
        )
        row = cur.fetchone()
        if row is None or row[0] != want_type:
            return False, f"schema_not_ready:payment_applications.{column}"
        # A nullable amount_minor or loan_id lets a row record an apply that credited
        # nothing, which is the state the record exists to make impossible.
        if row[1] != "NO":
            return False, f"schema_not_ready:payment_applications.{column}"
    # UNIQUE (payment_id) is the whole replay guard: without it the same payment applies
    # twice and credits twice. indpred IS NULL rejects a PARTIAL unique index of the same
    # shape, which would leave some rows unguarded while answering this probe.
    cur.execute(
        "SELECT count(*) FROM pg_index x "
        "  JOIN pg_class i ON i.oid = x.indexrelid "
        "  JOIN pg_namespace n ON n.oid = i.relnamespace "
        "  JOIN pg_class c ON c.oid = x.indrelid "
        # relname is unique per SCHEMA, not per database: an index of the same name in
        # another schema would otherwise answer this probe.
        " WHERE c.relname = 'payment_applications' AND n.nspname = current_schema() "
        "   AND x.indisunique AND x.indpred IS NULL "
        "   AND (SELECT string_agg(a.attname, ',' ORDER BY k.ord) "
        "          FROM unnest(x.indkey) WITH ORDINALITY AS k(attnum, ord) "
        "          JOIN pg_attribute a "
        "            ON a.attrelid = x.indrelid AND a.attnum = k.attnum) = 'payment_id'"
    )
    if (cur.fetchone() or (0,))[0] == 0:
        return False, "schema_not_ready:payment_applications.payment_id_uniq"
    return True, None


def _run_database_probe(timeout: float) -> tuple[bool, str | None]:
    if not DATABASE_URL:
        return False, "DATABASE_URL not set"
    conn = None
    try:
        conn = psycopg2.connect(
            DATABASE_URL,
            connect_timeout=max(1, int(timeout)),
            options="-c statement_timeout=2000",
        )
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
            ok, reason = _payments_idempotency_ready(cur)
            if not ok:
                return False, reason
            ok, reason = _payment_applications_ready(cur)
            if not ok:
                return False, reason
        return True, None
    except Exception as exc:
        return False, exc.__class__.__name__
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def missing_required_secrets() -> list:
    """Config that MUST be present for a healthy runtime; surfaced by /health.

    An unset or passwordless DATABASE_URL reports unhealthy instead of connecting
    unauthenticated. PROCESSOR_API_KEY is required too: without it this service
    cannot authorize a real capture, and the charge path would otherwise record a
    'captured' payment no processor ever saw — so a keyless deploy must read
    unhealthy and /payments must fail closed (see processor_configured)."""
    missing = []
    if not database_url_configured():
        missing.append("DATABASE_URL")
    if not processor_configured():
        missing.append("PROCESSOR_API_KEY")
    # servicing-service's apply-payment now requires X-Internal-Service (ADR 0014
    # Decision 1). Unset here means every captured charge silently fails to reduce
    # the loan balance (_apply_via_servicing swallows the 403) -- that must read as
    # unhealthy rather than surface only as a money/state divergence nobody sees.
    if not internal_service_token_configured():
        missing.append("INTERNAL_SERVICE_TOKEN")
    return missing


# Processor key — env only; no committed default. Rotate the previously-committed
# key (see docs/security-remediation-2026-07.md).
PROCESSOR_API_KEY = os.getenv("PROCESSOR_API_KEY", "")


def processor_configured() -> bool:
    """The card-processor credential must be present to authorize/capture a payment.
    After the secret purge there is no committed fallback, so an unset key means the
    service cannot legitimately capture — fail closed (readiness AND the /payments
    endpoint) rather than record a 'captured' payment no processor ever saw."""
    return bool(PROCESSOR_API_KEY)


PROCESSOR_BASE_URL = os.getenv(
    "PROCESSOR_BASE_URL", "https://api.cardprocessor.example.com"
)
# servicing-service base URL — we call it to apply a captured payment to the balance
SERVICING_URL = os.getenv("SERVICING_URL", "http://servicing-service:8002")

# Shared secret identifying this service to servicing-service's apply-payment route
# (ADR 0014 Decision 1). Env only, no committed default; unset makes apply-payment
# fail closed on servicing's side. Same variable name servicing-service reads.
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")


def internal_service_token_configured() -> bool:
    """The token must be set AND ASCII -- httpx encodes header values as ASCII by
    default, so a non-ASCII token raises UnicodeEncodeError inside
    _apply_via_servicing's httpx.post call. That exception is caught by the same
    broad except that handles servicing being unreachable, so a non-ASCII token
    would silently fall into the exact captured-but-not-applied failure this gate
    exists to close, with a log line that reads as an encoding error rather than a
    config problem. Fail closed here instead so it surfaces at /health."""
    return bool(INTERNAL_SERVICE_TOKEN) and INTERNAL_SERVICE_TOKEN.isascii()


LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")


# D19 replay window. Client answer 2026-08-17: "keep it configurable, keep 24 hours as
# the working value -- that number is our product choice, not an industry default. If
# real retry behaviour argues for a different figure later, that is a configuration
# change rather than a rework."
#
# The window governs how long a FINISHED payment's key stays claimable for replay, not
# how long the system waits for a payment: an intent still in flight keeps its key
# however old it is (an ACH row sits `submitted` for days), because releasing it would
# free the key for a new charge while the original is still live.
#
# Deliberately NOT in missing_required_secrets(): unlike PROCESSOR_API_KEY there is a
# correct default, so an unset value is not a misconfiguration and must not read as
# unhealthy. A non-numeric or non-positive value IS a misconfiguration and falls back to
# the default rather than silently disabling the window (a zero or negative TTL would
# expire every key instantly and reinstate the double charge).
PAYMENT_IDEMPOTENCY_TTL_HOURS_DEFAULT = 24


def payment_idempotency_ttl_hours() -> int:
    raw = os.getenv("PAYMENT_IDEMPOTENCY_TTL_HOURS", "")
    if not raw:
        return PAYMENT_IDEMPOTENCY_TTL_HOURS_DEFAULT
    try:
        hours = int(raw)
    except ValueError:
        return PAYMENT_IDEMPOTENCY_TTL_HOURS_DEFAULT
    return hours if hours > 0 else PAYMENT_IDEMPOTENCY_TTL_HOURS_DEFAULT
