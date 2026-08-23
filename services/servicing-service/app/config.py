import os
import re
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


# Schema rung for D19 (migration 0018). This service carries the SECOND charge handler
# (ADR 0004 copied it out of here into payment-service and left both routed, debt D23),
# so it claims the idempotency key with the same INSERT ... ON CONFLICT against a
# PARTIAL unique index. A volume without migration 0018 would raise "no unique or
# exclusion constraint matching the ON CONFLICT specification" on the first charge
# through this route — the double-charge control failing where it is first needed.
#
# The NAME of an index proves nothing: CREATE UNIQUE INDEX IF NOT EXISTS matches on
# name alone, so a same-named index that is non-unique, on the wrong column, or missing
# the partial predicate would leave the control disabled while reporting ready. Assert
# the definition — indisunique, the table, the covered column, and the predicate — and
# assert the column TYPE too, since ADD COLUMN IF NOT EXISTS swallows a same-named
# column of any type and a TEXT stand-in for BIGINT hands every reader a string.
#
# Kept byte-identical to payment-service's copy: both services write this table and both
# claim the key, so a rung that drifts between them would let one service report ready
# over a schema the other refuses.
_IDEMPOTENCY_COLUMNS = (
    ("idempotency_key", "text"),
    ("idempotency_expires_at", "timestamp with time zone"),
    ("request_fingerprint", "text"),
    ("status", "text"),
    ("processor_idempotency_key", "text"),
    ("amount_minor", "bigint"),
)
_IDEMPOTENCY_INDEXES = (
    ("payments_idempotency_key_uniq", "idempotency_key"),
    ("payments_processor_idempotency_key_uniq", "processor_idempotency_key"),
)


def _payments_idempotency_ready(cur) -> tuple[bool, str | None]:
    """Assert migration 0018 is actually applied, by definition and not by name."""
    for column, want_type in _IDEMPOTENCY_COLUMNS:
        cur.execute(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name = 'payments' AND column_name = %s",
            (column,),
        )
        row = cur.fetchone()
        if row is None or row[0] != want_type:
            return False, f"schema_not_ready:payments.{column}"
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
            "  JOIN pg_class c ON c.oid = x.indrelid "
            " WHERE i.relname = %s",
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
        # make that insert raise.
        if predicate is None or "IS NOT NULL" not in predicate:
            return False, f"schema_not_ready:{index}"
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
            # Schema rung: the loan read paths (list, detail, schedule) load the full
            # Loan entity, whose ORM mapping now includes loans.note_rate (migration
            # 0014 — servicing amortizes the schedule at the note rate, not the disclosed
            # APR). Migrations are hand-applied and lag the init DDL, so a volume that has
            # the loans table but not this column (predating 0014) would 500 EVERY servicing
            # loan read while /health otherwise read fine — the same class origination's
            # readiness rung guards for on its boarding INSERT. The type is asserted, not
            # just the name: ADD COLUMN IF NOT EXISTS swallows a same-named column of any
            # type, and the ORM maps it as a float.
            cur.execute(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name = 'loans' AND column_name = 'note_rate' "
                "AND data_type = 'double precision'"
            )
            if cur.fetchone() is None:
                return False, "schema_not_ready:loans.note_rate"
            ok, reason = _payments_idempotency_ready(cur)
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
    cannot authorize a real capture, and the direct /payments charge path would
    otherwise record a 'captured' payment (and mutate the balance) that no
    processor ever saw — so a keyless deploy must read unhealthy and /payments
    must fail closed (see processor_configured)."""
    missing = []
    if not database_url_configured():
        missing.append("DATABASE_URL")
    if not processor_configured():
        missing.append("PROCESSOR_API_KEY")
    # INTERNAL_SERVICE_TOKEN gates apply-payment and late-fee (ADR 0014 Decision 1).
    # Unset makes both routes fail closed with 503, so the LOS->LSS apply path stops
    # working — that must read as unhealthy here rather than surface as a per-request
    # 503 nobody attributes to config. Mirrors kyc/decision/disclosure.
    if not INTERNAL_SERVICE_TOKEN:
        missing.append("INTERNAL_SERVICE_TOKEN")
    # Reconciliation's duplicate-charge scan aborts without this (D2(e)); unguarded,
    # a deploy would pass /health and only fail when /reconciliation/peek is called —
    # the hidden-control-failure class this whole change exists to remove.
    if not duplicate_suspect_window_configured():
        missing.append("DUPLICATE_SUSPECT_WINDOW_SECONDS")
    # D4's alert threshold, same posture: unguarded, a deploy would pass /health and the
    # month-end run would abort — the operator learns the control was never configured at
    # the moment they need its answer.
    if not reconciliation_alert_threshold_configured():
        missing.append("RECONCILIATION_ALERT_THRESHOLD_MINOR")
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

# Shared secret identifying an internal service-to-service call. Required by
# apply-payment (called by payment-service after it captures a charge) and late-fee
# (rule-driven, no operator). Env only, no committed default; unset makes both routes
# fail closed. Same variable the five sibling services already read from .env.
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")
SETTLEMENT_FILE = os.getenv("SETTLEMENT_FILE", "data/settlement.csv")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Bound for reconciliation's duplicate-charge scan (spec D2(e)). No default: a guessed
# bound is worse than no detection, since a wrong one would report a false clean. Env
# only; reconciliation.py aborts (EXIT_ABORT) rather than run with it unset or invalid.
DUPLICATE_SUSPECT_WINDOW_SECONDS = os.getenv("DUPLICATE_SUSPECT_WINDOW_SECONDS", "")
# D2(e) says the bound is "scoped in minutes, not days". 30 days is far beyond that
# intent but still a sane ceiling against a misconfigured operator value. Single
# source of truth: reconciliation.py imports this rather than redeclaring it, so the
# /health check below and the abort it mirrors can never drift apart.
MAX_DUPLICATE_SUSPECT_WINDOW_SECONDS = 30 * 24 * 60 * 60


# Alert threshold for reconciliation (spec D4), in MINOR units — the client's answer of
# 2026-08-14 set it at $5.00 aggregate, so 500. No default, same reason as the bound
# above: a guessed threshold either alerts on everything or on nothing, and both read as
# a working control. Env only; reconciliation.py aborts (EXIT_ABORT) rather than run with
# it unset or invalid.
RECONCILIATION_ALERT_THRESHOLD_MINOR = os.getenv(
    "RECONCILIATION_ALERT_THRESHOLD_MINOR", ""
)

# D4's threshold, same posture as the money parsers it guards: `int()` alone accepts
# "1_000" and "+500", neither of which reads like a figure a person typed into a
# deploy. Single source of truth: reconciliation.py imports this rather than
# redeclaring it, so the /health check below and the abort it mirrors can never
# drift apart the way they did before (readiness took bare int(), runtime took this
# regex, and the two disagreed on "1_000" and "+500").
_PLAIN_INTEGER = re.compile(r"^\d+$")


def reconciliation_alert_threshold_configured() -> bool:
    """True only when RECONCILIATION_ALERT_THRESHOLD_MINOR is a plain positive integer.

    Zero is rejected rather than read as "alert on any variance": the one way variance
    is nonzero with an empty break list is a match pairing across the window edge, which
    is the settlement lag the tolerance absorbs by design, so a zero threshold would
    alert on ordinary cut-off timing every close. An operator who wants that should say
    so with a 1, not with a value that also reads as "unset".

    Same validity reconciliation.py's _alert_threshold_minor() enforces (same regex,
    imported from here), checked here so a misconfigured deploy fails /health rather
    than only failing on the first run.
    """
    raw = (RECONCILIATION_ALERT_THRESHOLD_MINOR or "").strip()
    if not raw or not _PLAIN_INTEGER.match(raw):
        return False
    return int(raw) > 0


def duplicate_suspect_window_configured() -> bool:
    """True only when DUPLICATE_SUSPECT_WINDOW_SECONDS is set to a positive integer
    at or under the ceiling above — the same validity reconciliation.py's
    _duplicate_suspect_window_seconds() enforces, checked here so an unset or
    malformed value fails /health instead of only failing on the first
    /reconciliation/peek call."""
    raw = (DUPLICATE_SUSPECT_WINDOW_SECONDS or "").strip()
    if not raw:
        return False
    try:
        seconds = int(raw)
    except ValueError:
        return False
    return 0 < seconds <= MAX_DUPLICATE_SUSPECT_WINDOW_SECONDS
