"""The runbook's reconciliation entry must describe the code that exists — spec §D6.

`docs/runbooks/operations.md` tells an operator what to run at month-end and what each break class
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

RUNBOOK = Path(__file__).resolve().parents[3] / "docs" / "runbooks" / "operations.md"
ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"

BREAK_CLASSES = (
    reconciliation.MISSING_IN_LEDGER,
    reconciliation.MISSING_IN_SETTLEMENT,
    reconciliation.REFUND_UNREPRESENTED,
    reconciliation.AMOUNT_MISMATCH,
    reconciliation.DUPLICATE_SUSPECT,
)


def _env_value(name: str) -> str:
    """The single declaration of `name` in `.env.example`, or fail loudly.

    Two declarations mean the file disagrees with itself and neither the runbook nor a
    deploy can be graded against it.
    """
    assert ENV_EXAMPLE.exists(), f".env.example not found at {ENV_EXAMPLE}"
    declared = [
        line.split("=", 1)[1].strip()
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line.startswith(f"{name}=")
    ]
    assert len(declared) == 1, (
        f"expected exactly one {name} in .env.example, found {len(declared)}"
    )
    return declared[0]


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


def test_the_alert_setting_and_its_value_are_documented(runbook):
    """D4. The alert is delivered through the exit code an operator's cron reads, so the
    setting that arms it, the figure it measures and the value it is measured against all
    have to be on the page they read at month-end. Value checked against `.env.example`
    for the same reason as the bound above."""
    assert "RECONCILIATION_ALERT_THRESHOLD_MINOR" in runbook
    assert "per-loan absolute variance" in runbook
    assert _env_value("RECONCILIATION_ALERT_THRESHOLD_MINOR") == "500"
    assert "`500`" in runbook


def test_the_runbook_does_not_promise_the_threshold_filters_the_report(runbook):
    """The client asked that individual unmatched transactions appear regardless of
    amount. A runbook implying the threshold suppresses small breaks would have an
    operator stop looking for them."""
    assert "gates the alert only" in runbook


def test_the_documented_window_value_matches_env_example(runbook):
    """Naming the variable is not enough — the VALUE has to agree with the file.

    The runbook cited 300 while `.env.example` shipped 120, so an operator following
    the runbook would set a bound two and a half times the one the repo ships, and the
    duplicate scan would report different pairs than every test and figure assumes.
    The class of defect this whole file exists to catch, on the one setting the job
    aborts without.

    Asserting the number appears beside the variable's name, not merely somewhere in
    the runbook, so an unrelated 120 elsewhere cannot satisfy it.
    """
    assert ENV_EXAMPLE.exists(), f".env.example not found at {ENV_EXAMPLE}"
    declared = [
        line.split("=", 1)[1].strip()
        for line in ENV_EXAMPLE.read_text().splitlines()
        if line.startswith("DUPLICATE_SUSPECT_WINDOW_SECONDS=")
    ]
    assert len(declared) == 1, (
        f"expected exactly one DUPLICATE_SUSPECT_WINDOW_SECONDS in .env.example, "
        f"found {len(declared)}"
    )
    value = declared[0]

    for line in runbook.splitlines():
        if "DUPLICATE_SUSPECT_WINDOW_SECONDS" in line and "`.env.example`" in line:
            assert f"`{value}`" in line, (
                f"runbook cites a different bound than .env.example ships "
                f"({value}): {line.strip()}"
            )
            break
    else:  # pragma: no cover - the test above already requires the name to appear
        pytest.fail("the runbook never states the bound's value against .env.example")


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


def test_finance_ops_is_named_as_the_owner_of_missing_in_ledger(runbook):
    """The client's 2026-08-14 answer, satisfied at document level or not at all.

    Asked who owned the $500 on loan 4471, she said: open exception, not a write-off,
    both processor references in the first exception report, marked for Finance Ops
    review. She then ruled out everything larger — "no separate remediation or
    ticketing workflow is needed in this build" — so there is no owner column and no
    status field to assert against. A runbook line IS the deliverable, which makes it
    exactly the kind of thing that gets dropped and never noticed.

    It was dropped: "Finance Ops" appeared nowhere in the repo for four days after the
    answer landed, while every other item that reply created shipped.
    """
    assert "Finance Ops" in runbook, (
        "the client asked for MISSING_IN_LEDGER breaks to be marked for Finance Ops "
        "review; naming the owner is the whole of that deliverable"
    )
    assert reconciliation.MISSING_IN_LEDGER in runbook


def test_the_runbook_does_not_present_the_alert_recipient_as_configured(runbook):
    """`ops@example.com` is a placeholder and must not read as an address.

    Who receives the alert was asked on 2026-08-12, bundled into one row with the
    cut-off convention. The cut-off half was answered on 08-14 and the recipient half
    was not, so the row scans as answered. An operator copying the cron example gets a
    control that detects correctly and reports to nobody.

    This asserts the runbook says so wherever it still shows the placeholder, rather
    than asserting the placeholder is gone — it is a legitimate example address.
    """
    if "ops@example.com" not in runbook:
        pytest.skip("placeholder recipient no longer present; a real one was set")
    assert "No recipient is configured" in runbook
