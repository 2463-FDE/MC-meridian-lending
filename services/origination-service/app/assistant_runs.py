"""One durable row per officer assistant request, for aggregate reporting.

The assistant is observable one run at a time and not in aggregate. `assistant.entry`
and `assistant.request` already carry the outcome enums, but `trace()` is a no-op unless
LANGSMITH_TRACING is set, so LangSmith's population is whatever happened to be exported
and no rate computed from it has a trustworthy denominator. `assistant.run()` writes
nothing to this database. This module is that denominator.

WHY THE WRITE LIVES AT THE ENTRY SPAN. Every refusal is translated in
`main._run_assistant`, not inside `assistant.run()` — by the time a refusal is known,
`run()` has already raised and its own `root.add_metadata` block never executed. A write
placed inside `run()` would therefore capture successes only and silently drop the entire
refusal population, which is the half an operator most needs to see.

CONTENT RULE, inherited unchanged from `app/assistant.py`. Everything written here is an
enum code, an integer, a boolean, or LangSmith's opaque run id. No prose, no provider
text, no exception string: `httpx.HTTPStatusError`'s message embeds the request URL
(which embeds the app_id) and an `LLMError` can carry raw provider text, so `str(exc)`
must never reach a column. `application_id` IS recorded, unlike on the spans — the
argument that strips it there is about an external telemetry vendor holding a value that
makes traces linkable to a customer, and it does not transfer to a table sitting beside
`applicants` in the same schema. See db/migrations/0021_assistant_runs.sql.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import db
from .logging_config import get_logger

log = get_logger("origination")

_INSERT = """
    INSERT INTO assistant_runs (
        trace_id, application_id, task, policy_topic, http_status, refusal_code,
        outcome, record_status, policy_band, narration_validated,
        policy_citations, policy_searches, latency_ms
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

_CODE_KEYS = ("outcome", "record_status", "policy_band")
_COUNT_KEYS = ("policy_citations", "policy_searches")


def _code(charted: dict, key: str) -> str | None:
    value = charted.get(key)
    return value if isinstance(value, str) and value else None


def _count(charted: dict, key: str) -> int | None:
    value = charted.get(key)
    # `isinstance(True, int)` is True, so bools are excluded explicitly rather than
    # arriving in an INTEGER column as 1/0.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def record(
    *,
    trace_id: str,
    application_id: int,
    task: str,
    policy_topic: str | None,
    http_status: int,
    refusal_code: str | None,
    charted: dict,
    latency_ms: int,
) -> None:
    """Write one row. NEVER raises.

    The assistant's answer is the product; this row is a measurement of it. A telemetry
    write that could 500 an officer's request would make the measurement more important
    than the thing measured, so every failure here is logged and swallowed — including
    the one that matters most in practice, a volume whose migration 0021 has not been
    applied. `config._run_database_probe` carries the readiness rung that surfaces that
    state at /health, so the silence is reported somewhere rather than merely tolerated.

    `charted` is the projection `main._charted` already computed for the span, passed in
    rather than recomputed: one definition of what may leave this service, used twice.
    """
    narration = charted.get("narration_validated")
    try:
        db.query(
            _INSERT,
            (
                trace_id,
                application_id,
                task,
                policy_topic,
                http_status,
                refusal_code,
                _code(charted, "outcome"),
                _code(charted, "record_status"),
                _code(charted, "policy_band"),
                narration if isinstance(narration, bool) else None,
                _count(charted, "policy_citations"),
                _count(charted, "policy_searches"),
                latency_ms,
            ),
        )
    except Exception as exc:
        # The class name only. The psycopg2 message for a constraint violation echoes the
        # offending row, which would put the recorded values into a log line.
        log.error("assistant_runs write failed: %s", exc.__class__.__name__)


# The windows an aggregate may be asked for. `window` reaches a Postgres `interval` cast,
# so it is the one injection surface this read has. It is bound as a parameter (SQLAlchemy
# renders a driver-level bind, never string interpolation) AND checked against this tuple,
# so no caller -- route, test, or a later in-process one -- can put an arbitrary string
# into the cast. A value off the list is refused; there is no silent fall back to the
# default, which would answer a different question than the one asked.
WINDOWS = ("1 day", "7 days", "30 days")

# --- The read boundary's own copy of the write path's vocabularies -------------------
#
# The CONTENT RULE at the top of this module is a rule about what the WRITE may store.
# Two of the four coded columns this aggregate serves are enforced in the database
# (`ck_assistant_runs_task`, `ck_assistant_runs_refusal_code`); `outcome` and
# `policy_band` are bare TEXT, so the rule holds for them only as long as `_code()`'s
# caller is the only writer. It is not the only conceivable one: a hand-applied fix, a
# backfill, or a volume restored from elsewhere can put arbitrary text in either column,
# and an aggregate that renders whatever it read would publish it to an officer -- a
# proven case, `outcome` carrying a borrower name and an SSN, served verbatim.
#
# So the read re-enforces the vocabularies rather than asserting the write did. A value
# off the list is replaced with UNRECOGNISED, not passed through and not raised on:
# masking keeps the aggregate readable and keeps the unconstrained value out of the
# response, which is the same fail-closed direction the redactor takes on a parse
# failure.
TASKS = ("decision", "explain")

# Every code the service can record, in the order `ck_assistant_runs_refusal_code`
# declares them. `test_the_refusal_vocabulary_matches_the_check_constraint` rebuilds that
# constraint's expected definition from this tuple, so widening one without the other is
# a failure rather than a read boundary that quietly masks a code the write accepts.
REFUSAL_CODES = (
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
)

# Both come from decision-service, a separate process with its own schema module, so
# neither can be imported and neither has a CHECK behind it here: `outcome` is
# db/init/001_schema.sql's `decisions.outcome` vocabulary, and `policy_band` is every
# value services/decision-service/app/model_vendor.py::policy_band can return. Widening
# either there without widening it here shows up as UNRECOGNISED rows in the aggregate,
# which is the intended failure: visible, and not a leak.
OUTCOMES = ("approve", "deny", "refer", "counteroffer")
POLICY_BANDS = ("approve", "refer", "deny")

# One token for "a coded column held a value this service does not recognise". Not None:
# NULL already means "the run did not carry this field" (`_charted` omits it), and fusing
# the two would read an unconstrained write as a served run that recorded no outcome.
UNRECOGNISED = "unrecognised"

# Bounds the GROUP BY. `http_status`, `outcome` and `policy_band` have no CHECK behind
# them, so the group count is not bounded by the vocabularies above -- an unconstrained
# writer can produce a row per distinct value. `ORDER BY runs DESC` means the groups that
# survive the cut are the ones an operator came for, and `truncated` on the response says
# the cut happened rather than letting a partial sum read as the whole window.
MAX_GROUPS = 500

# `statement_timeout` for this read only. Set with SET LOCAL inside the session's
# transaction, so it reverts when the transaction ends and cannot leak onto a pooled
# connection the next request checks out. Why a timeout at all: this is a full-window
# GROUP BY over a table with no retention policy (migration 0021's own tail records
# that), so its cost grows with the table and nothing else here would stop it.
STATEMENT_TIMEOUT_MS = 5000

_AGGREGATE = """
    SELECT task, http_status, refusal_code, outcome, policy_band,
           count(*) AS runs,
           percentile_disc(0.5)  WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
           percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
    FROM assistant_runs
    WHERE created_at >= now() - CAST(:window AS interval)
    GROUP BY task, http_status, refusal_code, outcome, policy_band
    ORDER BY runs DESC, task, http_status
    LIMIT :row_limit
"""


def aggregate(session: Session, window: str) -> tuple[list[dict], bool]:
    """Group the recorded runs in `window`. RAISES, unlike `record()`.

    Returns (groups, truncated). `truncated` is True when the window held more distinct
    groups than MAX_GROUPS, which is a claim about the numbers rather than a detail: the
    sums a caller computes from a truncated list describe part of the window, not all of
    it.

    The asymmetry with `record()` is deliberate. `record()` swallows because a telemetry
    write must never 500 an officer's answer. A read has no such duty, and a swallowed
    read returns a well-formed zero from a query that failed -- the silent-regression
    shape ADR 0015 names, and the one an operator cannot tell from a quiet week.

    Runs on the pooled SQLAlchemy session, NOT on `db.query`. `db.get_conn` is one
    module-global autocommit connection shared by every caller in the process, and
    psycopg2 serialises `execute()` on a connection, so a full-window GROUP BY there
    would block the raw-psycopg2 money paths -- intake, decisioning, boarding -- for as
    long as it ran. This is a read path, read paths in this service use SQLAlchemy
    (app/main.py's own module docstring), and a pooled connection is also what makes
    SET LOCAL a per-request setting rather than a process-wide one.

    `application_id` and `trace_id` are NEVER selected. Both are per-run, useless to an
    aggregate, and either one turns a PII-free response into rows linkable to a customer:
    `application_id` names the customer's application directly, and `trace_id` is the key
    into a vendor's copy of the same run. `idx_assistant_runs_created` covers the range
    scan; the GROUP BY sorts on top of it, which is fine at present volume and unmeasured
    beyond it.
    """
    if window not in WINDOWS:
        # Not an HTTPException: this is the module's invariant, held for every caller.
        # The route checks first and answers 422 with the vocabulary.
        raise ValueError(f"window must be one of {WINDOWS}")
    # Interpolated, not bound: SET takes no bind parameters. The value is this module's
    # own integer constant and never reaches here from a caller.
    session.execute(text(f"SET LOCAL statement_timeout = {int(STATEMENT_TIMEOUT_MS)}"))
    # One row more than the cap, so "there were more" is answered by the query rather
    # than inferred -- a list of exactly MAX_GROUPS is otherwise indistinguishable from a
    # window that happened to hold exactly that many.
    rows = (
        session.execute(
            text(_AGGREGATE), {"window": window, "row_limit": MAX_GROUPS + 1}
        )
        .mappings()
        .all()
    )
    return [dict(row) for row in rows[:MAX_GROUPS]], len(rows) > MAX_GROUPS
