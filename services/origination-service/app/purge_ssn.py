"""D33 SSN purge -- `python -m app.purge_ssn` (docs/debt-log.md, the GLBA handoff).

INERT BY DEFAULT. This is the mechanism, not the decision: it exists so the reversible
half of D33 (ssn_last4, KYC off the full value) does not have to wait on the client's
retention answer, but the one irreversible step -- actually nulling applicants.ssn --
needs that answer and an explicit human yes. Two independent gates stand between "this
file is on disk" and a row being purged:

  1. SSN_PURGE_ENABLED must be a truthy env var (config.py). Unset -- the shipped
     default -- means the mechanism is off no matter what flags are passed.
  2. --execute must be passed explicitly. Without it, every run is a dry run: it
     reports how many rows WOULD be purged and mutates nothing. --execute with the
     env gate off still dry-runs (and says why) -- it never silently no-ops without
     telling the caller what happened.

Nothing wires this into a scheduler (same posture as app/reconcile.py) -- an operator
invokes it by hand, or from their own cron once the retention answer lands.

Eligibility: applicants.created_at older than the configured window, ssn not already
NULL/empty. ssn_last4 is backfilled in the same UPDATE for any row that predates
migration 0023 -- a purge must never destroy the last-4 signal, only the full value.

An UPDATE does not erase the old row version; the CVV purge (migration 0020) rewrites
the table afterward (VACUUM FULL / pg_repack) for exactly that reason, and this
mechanism will need the same rewrite step once it is actually run. That step is
deliberately not in this file -- see docs/runbooks/operations.md for the operator
procedure once the retention answer lands and this is switched on.

Exit codes (mirrors app/reconcile.py D2(g)): 0 ran (dry or executed), 2 refused to run
(misconfigured window, bad --window-days, or a DB error).
"""

import argparse
import json
import sys

from . import config, db

EXIT_OK = 0
EXIT_ABORT = 2


class PurgeAbort(Exception):
    pass


def _configured_window_days() -> int:
    raw = config.SSN_PURGE_WINDOW_DAYS
    if not raw:
        raise PurgeAbort(
            "SSN_PURGE_WINDOW_DAYS is not set -- refusing to guess a retention window"
        )
    try:
        days = int(raw)
    except ValueError:
        raise PurgeAbort(f"SSN_PURGE_WINDOW_DAYS={raw!r} is not an integer")
    if days <= 0:
        raise PurgeAbort(f"SSN_PURGE_WINDOW_DAYS={raw!r} must be positive")
    return days


def eligible_count(window_days: int) -> int:
    rows = db.query(
        "SELECT count(*) AS n FROM applicants "
        "WHERE ssn IS NOT NULL AND ssn <> '' "
        "AND created_at < now() - (%s || ' days')::interval",
        (window_days,),
    )
    return rows[0]["n"] if rows else 0


def run(window_days: int, execute: bool) -> dict:
    """Dry-run unless BOTH execute=True and config.SSN_PURGE_ENABLED -- see module
    docstring. Always returns a result dict; never raises on a config gate being off,
    only on a DB error, so a caller always gets a report of what happened."""
    if not execute:
        return {
            "mode": "dry_run",
            "window_days": window_days,
            "eligible": eligible_count(window_days),
            "purged": 0,
        }
    if not config.SSN_PURGE_ENABLED:
        return {
            "mode": "dry_run",
            "window_days": window_days,
            "eligible": eligible_count(window_days),
            "purged": 0,
            "reason": "SSN_PURGE_ENABLED is not set -- --execute alone does not purge anything",
        }
    with db.transaction() as cur:
        cur.execute(
            "UPDATE applicants "
            "SET ssn_last4 = COALESCE(ssn_last4, RIGHT(ssn, 4)), ssn = NULL "
            "WHERE ssn IS NOT NULL AND ssn <> '' "
            "AND created_at < now() - (%s || ' days')::interval",
            (window_days,),
        )
        purged = cur.rowcount
    return {
        "mode": "executed",
        "window_days": window_days,
        "eligible": None,
        "purged": purged,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.purge_ssn",
        description=(
            "D33: purge applicants.ssn past the configured retention window. "
            "Dry-run unless --execute (and SSN_PURGE_ENABLED is set)."
        ),
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="actually purge (also needs SSN_PURGE_ENABLED=true) -- default is dry-run",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=None,
        help="override SSN_PURGE_WINDOW_DAYS for this run",
    )
    return parser


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        window_days = (
            args.window_days
            if args.window_days is not None
            else _configured_window_days()
        )
        if window_days <= 0:
            raise PurgeAbort(f"--window-days={window_days} must be positive")
        result = run(window_days, args.execute)
    except PurgeAbort as exc:
        print(f"ABORT: {exc}", file=sys.stderr)
        return EXIT_ABORT

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    print(
        f"{result['mode']} window_days={result['window_days']} purged={result['purged']}",
        file=sys.stderr,
    )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m app.purge_ssn`
    sys.exit(main())
