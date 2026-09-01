"""D3 -- offline judge unit tests (docs/specs/disclosure-narration-judge.md).

Runs `grade_fixture` on `FakeAdapter` -- no network, no tokens. Each case scripts the
TWO calls a single fixture makes, in order: `disclosure_narrate`, then
`disclosure_narrate_judge`.
"""

import json

from app.llm import ClaudeClient, FakeAdapter
from app.llm.config import LLMConfig
from app.llm.errors import LLMTimeoutError
from app.narration_judge import grade_fixture
from app.prompts import get_prompt

from .fixtures.disclosure_narration_fixtures import NARRATION_FIXTURES

# The D2 shape, verbatim -- `id` and `description` included, because that is what
# `grade_fixture` has to consume (see test_grades_a_real_pinned_fixture below).
FIXTURE = {
    "id": "normal_short_term",
    "description": "Hand-built twin of the first pinned fixture.",
    "application_id": 990001,
    "term_months": 24,
    "note_rate_pct": 7.99,
    "checks_passed": 5,
    "expected_officer_action": "review_and_send",
}


def _client(responses) -> ClaudeClient:
    config = LLMConfig(api_key="test-key", model="claude-test", max_tokens=512)
    return ClaudeClient(config, adapter=FakeAdapter(responses=list(responses)))


def _narration(summary: str, officer_action: str = "review_and_send") -> str:
    return json.dumps({"summary": summary, "officer_action": officer_action})


def _judge(grounded: bool) -> str:
    return json.dumps({"grounded": grounded})


def test_grounded_narration_matching_action_passes():
    client = _client(
        [
            _narration("Term is 24 months at 7.99% APR. Review and send."),
            _judge(True),
        ]
    )
    verdict = grade_fixture(client, FIXTURE)
    assert verdict.passed
    assert verdict.grounded_by_d1 is True
    assert verdict.grounded_by_judge is True
    assert verdict.officer_action_match is True
    assert verdict.error is None


def test_fabricated_figure_fails_even_though_d1_and_judge_agree():
    """D1 and the judge both catch it -- not a disagreement, an axis-(a) failure."""
    client = _client(
        [
            _narration("Monthly payment is $340."),
            _judge(False),
        ]
    )
    verdict = grade_fixture(client, FIXTURE)
    assert not verdict.passed
    assert verdict.grounded_by_d1 is False
    assert verdict.grounded_by_judge is False


def test_d1_judge_disagreement_fails_the_fixture():
    """A paraphrase D1's regex passes but the judge catches (or vice versa) is the
    exact regex-hole D1's own Risks section predicts -- axis (c).
    """
    client = _client(
        [
            _narration("Term is 24 months at 7.99% APR."),  # D1: grounded
            _judge(False),  # judge disagrees
        ]
    )
    verdict = grade_fixture(client, FIXTURE)
    assert not verdict.passed
    assert verdict.grounded_by_d1 is True
    assert verdict.grounded_by_judge is False


def test_misrouted_officer_action_fails_when_expected_is_pinned():
    client = _client(
        [
            _narration(
                "Term is 24 months at 7.99% APR.", officer_action="hold_for_compliance"
            ),
            _judge(True),
        ]
    )
    verdict = grade_fixture(client, FIXTURE)
    assert not verdict.passed
    assert verdict.officer_action_match is False


def test_ungraded_officer_action_fixture_does_not_fail_on_mismatch():
    """Rate-driven / checks_passed fixtures carry `expected_officer_action: None` --
    the spec rules that axis not gradeable for them (D2), so any action passes.
    """
    fixture = {**FIXTURE, "expected_officer_action": None}
    client = _client(
        [
            _narration(
                "Term is 24 months at 7.99% APR.", officer_action="hold_for_compliance"
            ),
            _judge(True),
        ]
    )
    verdict = grade_fixture(client, fixture)
    assert verdict.passed
    assert verdict.officer_action_match is None


def test_narrate_call_failure_fails_closed():
    client = _client([])
    client.adapter._raises = [LLMTimeoutError("boom")]
    verdict = grade_fixture(client, FIXTURE)
    assert not verdict.passed
    assert verdict.error is not None
    assert verdict.grounded_by_judge is None


def test_judge_call_failure_fails_closed():
    """The narrate call succeeds; only the second (judge) call fails."""
    responses = [_narration("Term is 24 months at 7.99% APR.")]

    def _on_complete(req):
        if responses:
            from app.llm.adapter import Completion

            return Completion(text=responses.pop(0), model=req.model)
        raise LLMTimeoutError("boom")

    config = LLMConfig(api_key="test-key", model="claude-test", max_tokens=512)
    client = ClaudeClient(config, adapter=FakeAdapter(on_complete=_on_complete))
    verdict = grade_fixture(client, FIXTURE)
    assert not verdict.passed
    assert verdict.error is not None
    assert verdict.grounded_by_d1 is True
    assert verdict.grounded_by_judge is None


def test_grades_a_real_pinned_fixture():
    """The harness must consume a D2 entry as it is actually written.

    `NARRATION_FIXTURES` entries carry `id` and `description`; a keyword signature
    taking `fixture_id` raises TypeError on its own input, and every other test here
    hand-builds a dict, so nothing else would notice.
    """
    fixture = NARRATION_FIXTURES[0]
    client = _client(
        [
            _narration("Term is 24 months at 7.99% APR. Review and send."),
            _judge(True),
        ]
    )
    verdict = grade_fixture(client, fixture)
    assert verdict.fixture_id == fixture["id"]
    assert verdict.passed
    assert verdict.error is None


def test_judge_is_given_the_same_context_as_the_narrator():
    """Axes (a) and (c) both misread if the judge is told the narrator got only two
    numbers: `disclosure_narrate` is also handed the application id and the passed check
    count, and is asked to name the loan, so a judge without them grades supplied context
    as fabrication.
    """
    fixture = NARRATION_FIXTURES[0]
    client = _client(
        [
            _narration("Application 990001: 24 months at 7.99% APR, 5 checks passed."),
            _judge(True),
        ]
    )
    grade_fixture(client, fixture)

    judge_request = client.adapter.calls[1]
    user_message = judge_request.messages[-1]["content"]
    assert str(fixture["application_id"]) in user_message
    assert f"({fixture['checks_passed']} deterministic checks)" in user_message
    assert set(get_prompt("disclosure_narrate_judge").required_vars) >= {
        "application_id",
        "checks_passed",
    }
