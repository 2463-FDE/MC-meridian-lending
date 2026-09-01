"""D3 -- offline LLM judge for disclosure narration groundedness.

docs/specs/disclosure-narration-judge.md, Minimum Build Slice #3. Grades the RAW
`disclosure_narrate` completion -- before D1's runtime guard (`_narration_is_grounded`,
`disclosure_coordinator.py`) can discard an ungrounded summary -- against a pinned
fixture (D2, `tests/fixtures/disclosure_narration_fixtures.py`). Grading D1's OUTPUT
instead would pass every fixture whether or not the checker prompt regressed, because a
rejected summary is replaced by the canned `NARRATION_UNAVAILABLE` brief before this
harness could ever see it; driving the prompt directly is what gives the gate teeth.

Three axes per fixture:

    (a) groundedness    -- the offline judge's verdict on the raw `summary`
    (b) officer_action  -- the completion's own `officer_action` against the fixture's
                            `expected_officer_action` (D2's term cutoff only; a fixture
                            with `expected_officer_action=None` is not graded on this axis)
    (c) agreement        -- D1's deterministic guard, run on the same raw completion,
                            must agree with axis (a); a disagreement is the regex hole
                            D1's own Risks section predicts

Fails closed: a `disclosure_narrate` or judge call that raises, or a judge reply that
fails schema validation, marks the fixture FAILED -- never skipped (same rule
reconciliation and atomic-apply hold on their own inputs).
"""

from __future__ import annotations

from dataclasses import dataclass

from .disclosure_coordinator import _narration_is_grounded
from .llm import ClaudeClient
from .llm.errors import LLMError, ValidationFailed


@dataclass(frozen=True)
class NarrationJudgeVerdict:
    """One fixture's graded result. `error` is set only when a call itself failed --
    never when a call succeeded and simply disagreed with the fixture's expectation.
    """

    fixture_id: str
    grounded_by_d1: bool | None
    grounded_by_judge: bool | None
    officer_action: str | None
    officer_action_expected: str | None
    officer_action_match: bool | None
    error: str | None

    @property
    def passed(self) -> bool:
        """False on a fabricated figure (axis a), a misrouted action (axis b), a
        D1/judge disagreement (axis c), or any call failure -- see module docstring.
        """
        if self.error is not None:
            return False
        if self.grounded_by_judge is not True:
            return False
        if self.grounded_by_judge != self.grounded_by_d1:
            return False
        if (
            self.officer_action_expected is not None
            and self.officer_action_match is not True
        ):
            return False
        return True


def grade_fixture(
    client: ClaudeClient,
    *,
    fixture_id: str,
    application_id: int,
    term_months: int,
    note_rate_pct: float,
    checks_passed: int,
    expected_officer_action: str | None,
) -> NarrationJudgeVerdict:
    """Grade one fixture end to end: real `disclosure_narrate` call, then the judge."""
    try:
        narration = client.complete(
            "disclosure_narrate",
            application_id=application_id,
            term_months=term_months,
            note_rate_pct=note_rate_pct,
            checks_passed=checks_passed,
        )
    except (LLMError, ValidationFailed) as exc:
        return NarrationJudgeVerdict(
            fixture_id=fixture_id,
            grounded_by_d1=None,
            grounded_by_judge=None,
            officer_action=None,
            officer_action_expected=expected_officer_action,
            officer_action_match=None,
            error=f"disclosure_narrate call failed: {type(exc).__name__}",
        )

    summary = narration["summary"]
    officer_action = narration["officer_action"]
    grounded_by_d1 = _narration_is_grounded(
        summary, term_months=term_months, note_rate_pct=note_rate_pct
    )
    officer_action_match = (
        officer_action == expected_officer_action
        if expected_officer_action is not None
        else None
    )

    try:
        judge = client.complete(
            "disclosure_narrate_judge",
            summary=summary,
            term_months=term_months,
            note_rate_pct=note_rate_pct,
        )
    except (LLMError, ValidationFailed) as exc:
        return NarrationJudgeVerdict(
            fixture_id=fixture_id,
            grounded_by_d1=grounded_by_d1,
            grounded_by_judge=None,
            officer_action=officer_action,
            officer_action_expected=expected_officer_action,
            officer_action_match=officer_action_match,
            error=f"judge call failed: {type(exc).__name__}",
        )

    return NarrationJudgeVerdict(
        fixture_id=fixture_id,
        grounded_by_d1=grounded_by_d1,
        grounded_by_judge=judge["grounded"],
        officer_action=officer_action,
        officer_action_expected=expected_officer_action,
        officer_action_match=officer_action_match,
        error=None,
    )
