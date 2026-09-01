"""D33 SSN purge -- `python -m app.purge_ssn` (docs/debt-log.md, the GLBA handoff).

INERT BY DEFAULT. This is the mechanism, not the decision: it exists so the reversible
half of D33 (ssn_last4, KYC off the full value) does not have to wait on the client's
retention answer, but the one irreversible step -- actually nulling applicants.ssn --
needs that answer and an explicit human yes. Three independent gates stand between "this
file is on disk" and a row being purged:

  1. SSN_PURGE_ENABLED must be a truthy env var (config.py). Unset -- the shipped
     default -- means the mechanism is off no matter what flags are passed.
  2. --execute must be passed explicitly. Without it, every run is a dry run: it
     reports how many rows WOULD be purged and mutates nothing. --execute with the
     env gate off still dry-runs (and says why) -- it never silently no-ops without
     telling the caller what happened.
  3. _ELIGIBILITY_IS_PLACEHOLDER must be cleared IN CODE. Gates 1 and 2 are both
     operator-flippable -- an env var and a CLI flag -- so on their own the only thing
     standing between the known-wrong query below and a live run is that somebody read
     this docstring. A comment cannot refuse. While the constant stands, an --execute
     run with the env gate open ABORTS (exit 2) instead of purging; clearing it is a
     reviewed code change, which is the same weight as the defect it is holding back.

Nothing wires this into a scheduler (same posture as app/reconcile.py) -- an operator
invokes it by hand, or from their own cron once the retention answer lands.

KNOWN-WRONG SHAPE, DO NOT ENABLE AS-IS (docs/handoffs/2026-08-31-docs-glba-encryption-
framing.md, "The purge is not migration 0020's shape"). Eligibility here is calendar
age since applicants.created_at. That is wrong: the bureau pull needs the real digits
while an application is still decisionable, so the trigger has to be the application
reaching a TERMINAL state (decided/funded/declined), not elapsed time -- and one
applicant can have more than one applications row, so the real rule is "purge only
once every application tied to this applicant is terminal", which this file does not
check. The exact trigger also still depends on the client's retention-window answer
(the handoff's blocker #1) -- terminal-state-only vs. terminal-state-plus-a-grace-
period are different migrations, so this shape should not be finalized twice. This
file ships the safety gates and the dry-run reporting shape; the WHERE clause is a
placeholder and must be rewritten to an applications-status join, and
_ELIGIBILITY_IS_PLACEHOLDER cleared in the same change, before this can purge anything.

Eligibility (placeholder, see above): applicants.created_at older than the configured
window, ssn not already NULL/empty. ssn_last4 is backfilled in the same UPDATE for any
row that predates migration 0023 -- a purge must never destroy the last-4 signal, only
the full value.

An UPDATE does not erase the old row version; the CVV purge (migration 0020) rewrites
the table afterward (VACUUM FULL / pg_repack) for exactly that reason, and this
mechanism will need the same rewrite step once it is actually run. That step is
deliberately not in this file -- see docs/runbooks/operations.md for the operator
procedure once the retention answer lands and this is switched on.

Exit codes (mirrors app/reconcile.py D2(g)): 0 ran (dry or executed), 2 refused to run
(placeholder eligibility, misconfigured window, bad --window-days, or a DB error). A
refusal is never a clean 0 -- could-not-run must not read as ran-clean.
"""

import argparse
import json
import sys

from . import config, db

EXIT_OK = 0
EXIT_ABORT = 2

# Gate 3 (see the module docstring). Clear this ONLY in the same change that replaces
# the calendar-age WHERE clause below with the applications-terminal-state join. It is a
# module constant rather than another env var on purpose: the other two gates can be
# flipped by whoever runs the command, and this one cannot be flipped without a diff.
_ELIGIBILITY_IS_PLACEHOLDER = True


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
    docstring. Returns a result dict for every path that could legitimately run; a config
    gate being off is reported, not raised, so a caller always learns what happened.
    RAISES PurgeAbort on the placeholder interlock (gate 3), because there the caller
    asked for a mutation this file must not perform -- reporting a 0-row success would
    be indistinguishable from "ran clean, nothing was eligible"."""
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
    # Checked AFTER the env gate so an operator who has not opted in still gets the
    # specific "you did not set SSN_PURGE_ENABLED" report, and this refusal fires only on
    # a run that would otherwise have mutated rows. Raises rather than returning a report:
    # the caller asked to purge, this cannot honour that, and a 0 exit over a no-op would
    # read as "ran clean, nothing eligible".
    if _ELIGIBILITY_IS_PLACEHOLDER:
        raise PurgeAbort(
            "eligibility query is a known-wrong placeholder (calendar age, not "
            "application terminal state) -- refusing to purge. See the module docstring; "
            "clear _ELIGIBILITY_IS_PLACEHOLDER in the change that fixes the WHERE clause."
        )
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
