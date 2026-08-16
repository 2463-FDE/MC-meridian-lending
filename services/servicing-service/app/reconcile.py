"""The break report entrypoint — `python -m app.reconcile --from DATE --to DATE`.

Spec: `docs/spec-observability-week7.md` §D3. Runnable inside the container and in CI.

**Schedulable, not scheduled.** There is no scheduler in this stack — compose runs no
cron and `db/migrations` are hand-applied — and this does not introduce one. What ships
is deterministic exit codes, no interactive input and no state between runs, so the
operator's existing cron can run it; the runbook (D6) carries the crontab line. Building
a scheduler here would be more work than the control it exists to trigger, and a
scheduler that silently stops is the same defect class as a comparison that silently
reads nothing.

Output is split by stream on purpose: the JSON document on stdout so a piped run is
parseable, the human summary on stderr so a terminal run is readable. An abort writes
NO document at all — a caller piping stdout into a dashboard must never receive a
well-formed report from a run that read nothing.

Exit codes (D2(g)): 0 reconciled/no breaks, 1 breaks found, 2 could not run.
"""

import argparse
import json
import sys
from datetime import datetime

from . import reconciliation

_DEPENDS = "depends on the matching tolerance"
_STABLE = "does not depend on the matching tolerance"


def _parse_date(raw: str):
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise reconciliation.ReconciliationAbort(f"date {raw!r} is not YYYY-MM-DD")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.reconcile",
        description="Reconcile the payments table against the processor settlement file.",
    )
    parser.add_argument("--from", dest="from_date", help="window start, YYYY-MM-DD")
    parser.add_argument("--to", dest="to_date", help="window end, YYYY-MM-DD")
    parser.add_argument(
        "--tolerance-days",
        type=int,
        default=reconciliation.MATCH_TOLERANCE_DAYS,
        help=(
            "settlement-date tolerance when matching, in days "
            f"(default {reconciliation.MATCH_TOLERANCE_DAYS}); "
            "moves the gross break value, never the net variance"
        ),
    )
    return parser


def format_summary(result: reconciliation.ReconciliationResult) -> str:
    """The human half, for stderr. States all three figures with their dependence.

    A reader of the terminal output must not have to infer which figures move with a
    constant we chose (D3(c)).
    """
    lines = [
        f"window {result.window_from}..{result.window_to}  "
        f"tolerance +/-{result.tolerance_days}d",
        f"  ledger      {result.ledger_row_count:>3} rows  {result.ledger_total_minor:>9} minor",
        f"  settlement  {result.settlement_row_count:>3} rows  "
        f"{result.settlement_net_minor:>9} minor net "
        f"({result.settlement_captures_minor} captured, "
        f"{result.settlement_refunds_minor} refunded)",
        f"  matched     {result.matched_count:>3} rows",
        "",
        f"breaks {len(result.breaks)}",
    ]
    counts: dict = {}
    for item in result.breaks:
        entry = counts.setdefault(item.break_class, [0, 0])
        entry[0] += 1
        entry[1] += item.amount_minor
    for break_class in sorted(counts):
        rows, minor = counts[break_class]
        lines.append(f"  {break_class:<22} {rows:>3} rows  {minor:>9} minor")

    lines.append(
        f"duplicate suspects {len(result.duplicates)} (signal only, not a variance)"
    )
    for item in result.duplicates:
        lines.append(
            f"  loan {item.loan_id}  {item.amount_minor} minor  {item.gap_seconds}s apart"
            f"  payments {item.first_payment_id},{item.second_payment_id}"
        )

    # D4. Above the figures, not below them: an operator scanning the tail of a cron
    # mail must not have to read three variance lines to learn whether it fired.
    if result.alert_triggered:
        lines += [
            "",
            f"ALERT: per-loan absolute variance {result.per_loan_absolute_minor} minor "
            f"exceeds the {result.alert_threshold_minor} minor threshold",
        ]
    else:
        lines += [
            "",
            f"no alert: per-loan absolute variance {result.per_loan_absolute_minor} "
            f"minor is within the {result.alert_threshold_minor} minor threshold",
        ]

    lines += [
        "",
        f"  net variance               {result.net_variance_minor:>9} minor   {_STABLE}",
        f"  per-loan absolute variance {result.per_loan_absolute_minor:>9} minor   {_STABLE}",
        f"  gross break value          {result.gross_break_minor:>9} minor   {_DEPENDS}",
        "",
        "The net nets opposing errors against each other; the per-loan absolute does not.",
        "Where those two differ, the difference is error the net is hiding.",
        f"exit {result.exit_code}",
    ]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        from_date = _parse_date(args.from_date) if args.from_date else None
        to_date = _parse_date(args.to_date) if args.to_date else None
        result = reconciliation.reconcile(
            from_date=from_date,
            to_date=to_date,
            tolerance_days=args.tolerance_days,
        )
    except reconciliation.ReconciliationAbort as exc:
        # Nothing on stdout: "could not check" is never a document.
        print(f"ABORT: {exc}", file=sys.stderr)
        return reconciliation.EXIT_ABORT

    json.dump(reconciliation.build_report(result), sys.stdout, indent=2)
    sys.stdout.write("\n")
    print(format_summary(result), file=sys.stderr)
    return result.exit_code


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m app.reconcile`
    sys.exit(main())
