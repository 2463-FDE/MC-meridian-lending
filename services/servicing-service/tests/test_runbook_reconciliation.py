"""The runbook's reconciliation entry must describe the code that exists — spec §D6.

`docs/runbook.md` tells an operator what to run at month-end and what each break class
means. Those names and exit codes live in `app/reconciliation.py`, so the two drift
apart silently: renaming a break class or adding a fourth exit code leaves the runbook
confidently wrong, and the operator reading it at month-end is the one who finds out.

This is the same guard the repo already applies to `policies/fee_schedule.json` against
`policies/fee_schedule.md` — the doc is checked against the code, not trusted.

Scope: the operator-facing contract only (invocation, exit codes, break classes, the
fail-closed setting). It does not grade prose.
"""

from pathlib import Path

import pytest

from app import reconciliation

RUNBOOK = Path(__file__).resolve().parents[3] / "docs" / "runbook.md"

BREAK_CLASSES = (
    reconciliation.MISSING_IN_LEDGER,
    reconciliation.MISSING_IN_SETTLEMENT,
    reconciliation.REFUND_UNREPRESENTED,
    reconciliation.AMOUNT_MISMATCH,
    reconciliation.DUPLICATE_SUSPECT,
)


@pytest.fixture(scope="module")
def runbook():
    assert RUNBOOK.exists(), f"runbook not found at {RUNBOOK}"
    return RUNBOOK.read_text()


def test_the_invocation_is_documented(runbook):
    """D6 — the command an operator actually types."""
    assert "python -m app.reconcile" in runbook


def test_every_break_class_is_documented(runbook):
    """A class the operator cannot look up is a break they cannot action."""
    missing = [name for name in BREAK_CLASSES if name not in runbook]
    assert not missing, f"break classes absent from the runbook: {missing}"


def test_every_exit_code_is_documented_with_its_meaning(runbook):
    """D2(g) — the 0/1/2 split is the whole point of a cron-driven control.

    An operator whose cron treats 2 as success has a check that reports clean on runs
    that read nothing, which is the defect this control exists to prevent.
    """
    for code in (
        reconciliation.EXIT_CLEAN,
        reconciliation.EXIT_BREAKS,
        reconciliation.EXIT_ABORT,
    ):
        assert f"`{code}`" in runbook, f"exit code {code} is not documented"
    assert "ABORT" in runbook


def test_the_fail_closed_setting_is_named(runbook):
    """The job exits 2 without it, so an operator must be told it exists."""
    assert "DUPLICATE_SUSPECT_WINDOW_SECONDS" in runbook


def test_the_runbook_does_not_reference_the_deleted_total_helpers(runbook):
    """D3(d) — `ledger_total`/`settlement_total` no longer exist.

    The stale month-end entry described exactly those two, so this asserts the entry
    was REPLACED rather than appended to.
    """
    assert "ledger_total" not in runbook
    assert "settlement_total" not in runbook
    assert "reconciliation.peek` totals do not tie out" not in runbook


def test_the_report_is_not_described_as_scheduled(runbook):
    """D3(a) — what ships is schedulable, not scheduled.

    This stack runs no scheduler and this work did not add one. A runbook promising a
    daily job that nothing triggers is the same failure as a comparison that reads
    nothing: it reports coverage it does not have.
    """
    assert "cron" in runbook.lower(), "the operator must be told they wire the schedule"
