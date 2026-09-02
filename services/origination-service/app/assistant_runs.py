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
# so it is the one injection surface this read has. It is bound as a parameter (psycopg2
# renders a quoted literal, never string interpolation) AND checked against this tuple, so
# no caller -- route, test, or a later in-process one -- can put an arbitrary string into
# the cast. A value off the list is refused; there is no silent fall back to the default,
# which would answer a different question than the one asked.
WINDOWS = ("1 day", "7 days", "30 days")

_AGGREGATE = """
    SELECT task, http_status, refusal_code, outcome, policy_band,
           count(*) AS runs,
           percentile_disc(0.5)  WITHIN GROUP (ORDER BY latency_ms) AS p50_ms,
           percentile_disc(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
    FROM assistant_runs
    WHERE created_at >= now() - %s::interval
    GROUP BY task, http_status, refusal_code, outcome, policy_band
    ORDER BY runs DESC, task, http_status
"""


def aggregate(window: str) -> list[dict]:
    """Group the recorded runs in `window`. RAISES, unlike `record()`.

    The asymmetry is deliberate. `record()` swallows because a telemetry write must never
    500 an officer's answer. A read has no such duty, and a swallowed read returns a
    well-formed zero from a query that failed -- the silent-regression shape ADR 0015
    names, and the one an operator cannot tell from a quiet week.

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
    return db.query(_AGGREGATE, (window,))
