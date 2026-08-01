"""Disclosure pipeline: routing, the bounded cycle, and the gate that can actually fail.

Runs the real compiled LangGraph on FakeAdapter — no network, no database, no tokens. The
service calls are injected, so what is exercised is the graph itself: which nodes run, in
what order, and what the conditional edges decide.
"""

import json

import pytest

from app.disclosure_coordinator import (
    MAX_ASSEMBLE_ATTEMPTS,
    BlockReason,
    LangGraphDisclosureCoordinator,
)
from app.llm import ClaudeClient, FakeAdapter
from app.llm.config import LLMConfig

FIGURES = {
    "apr": "9.584",
    "finance_charge": "3628.71",
    "amount_financed": "17460.00",
    "total_of_payments": "21088.71",
    "monthly_payment": "439.35",
}

APPROVED = {
    "outcome": "approve",
    "decision_event_id": 7,
    "principal": 18000.0,
    "annual_rate": 7.99,
    "term_months": 48,
}


def _document(figures=None) -> str:
    return json.dumps(
        {
            "heading": "Truth in Lending Disclosure",
            "figures": figures or FIGURES,
            "payment_terms": "48 monthly payments.",
            "prepayment": "No penalty for early payoff.",
        }
    )


def _narration() -> str:
    return json.dumps(
        {"summary": "Disclosure ready for review.", "officer_action": "review_and_send"}
    )


def _client(responses) -> ClaudeClient:
    config = LLMConfig(api_key="test-key", model="claude-test", max_tokens=512)
    return ClaudeClient(config, adapter=FakeAdapter(responses=list(responses)))


def _coordinator(responses, *, context=None, offer=None, persisted=None):
    calls = {"compute": 0, "persist": 0}

    def compute_offer(payload):
        calls["compute"] += 1
        return (
            offer
            if offer is not None
            else {"offer_id": 11, "disclosure": dict(FIGURES)}
        )

    def persist(payload):
        calls["persist"] += 1
        return (
            persisted
            if persisted is not None
            else {"disclosure_id": 5, "status": "draft"}
        )

    coordinator = LangGraphDisclosureCoordinator(
        _client(responses),
        gather=lambda app_id: dict(context if context is not None else APPROVED),
        compute_offer=compute_offer,
        persist_disclosure=persist,
    )
    return coordinator, calls


def test_approved_application_runs_the_full_pipeline():
    coordinator, calls = _coordinator([_document(), _narration()])
    result = coordinator.run(1)

    assert result["status"] == "ok"
    assert result["attempts"] == 1
    assert result["document"]["figures"] == FIGURES
    assert result["narration"]["officer_action"] == "review_and_send"
    assert result["disclosure"] == {"disclosure_id": 5, "status": "draft"}
    assert calls == {"compute": 1, "persist": 1}


@pytest.mark.parametrize("outcome", ["refer", "deny", "counteroffer", None])
def test_only_an_approved_application_enters_the_pipeline(outcome):
    """Stage 0 exits for everything else — refer goes to manual review, deny to adverse
    action (roadmap), counteroffer cannot be emitted at all."""
    coordinator, calls = _coordinator([], context={**APPROVED, "outcome": outcome})
    result = coordinator.run(1)

    assert result["status"] == "blocked"
    assert result["reason"] == BlockReason.NOT_APPROVED
    assert calls == {"compute": 0, "persist": 0}, "must not compute or persist"


def test_missing_provenance_edge_blocks_before_any_llm_call():
    """A disclosure that cannot be traced to its decision is the gap ADR 0012 closes."""
    coordinator, calls = _coordinator(
        [], context={**APPROVED, "decision_event_id": None}
    )
    result = coordinator.run(1)

    assert result["reason"] == BlockReason.PROVENANCE_INCOMPLETE
    assert calls["compute"] == 0


def test_render_mismatch_retries_the_maker_then_succeeds():
    """The one automated cycle: the numbers were right, the document drifted."""
    drifted = {**FIGURES, "monthly_payment": "439.00"}
    coordinator, calls = _coordinator([_document(drifted), _document(), _narration()])
    result = coordinator.run(1)

    assert result["status"] == "ok"
    assert result["attempts"] == 2, "should have re-run the maker exactly once"
    assert calls["persist"] == 1
    # Recomputation happens once; the retry re-renders, it does not re-derive.
    assert calls["compute"] == 1


def test_persistent_render_mismatch_exhausts_the_bound_and_blocks():
    drifted = {**FIGURES, "apr": "9.58"}
    coordinator, calls = _coordinator(
        [_document(drifted)] * (MAX_ASSEMBLE_ATTEMPTS + 2)
    )
    result = coordinator.run(1)

    assert result["status"] == "blocked"
    assert result["reason"] == BlockReason.RETRIES_EXHAUSTED
    assert result["attempts"] == MAX_ASSEMBLE_ATTEMPTS
    assert calls["persist"] == 0, "a blocked document must never be persisted"


def test_a_wrong_number_is_never_persisted():
    """The gate compares against the deterministically computed figures, so a document
    the model 'improved' cannot reach the record."""
    invented = {**FIGURES, "apr": "5.041"}  # the old add-on value
    coordinator, calls = _coordinator([_document(invented)] * 4)
    result = coordinator.run(1)

    assert result["status"] == "blocked"
    assert calls["persist"] == 0


def test_the_checker_never_runs_on_a_failed_gate():
    """4b explains a verdict; it cannot overturn one. If it ran on failure, a permissive
    model response would be the last word on a wrong document."""
    drifted = {**FIGURES, "apr": "1.000"}
    responses = [_document(drifted)] * (MAX_ASSEMBLE_ATTEMPTS + 1)
    coordinator, _ = _coordinator(responses)
    coordinator.run(1)

    prompts_used = [
        call.messages[0]["content"] if call.messages else ""
        for call in coordinator.client.adapter.calls
    ]
    assert len(coordinator.client.adapter.calls) == MAX_ASSEMBLE_ATTEMPTS
    assert not any("brief the officer" in p.lower() for p in prompts_used)


def test_missing_figure_from_compute_blocks_without_calling_the_maker():
    coordinator, calls = _coordinator(
        [], offer={"offer_id": 11, "disclosure": {"apr": "9.584"}}
    )
    result = coordinator.run(1)

    assert result["status"] == "blocked"
    assert result["reason"] == BlockReason.NUMBER_WRONG
    assert "finance_charge" in result["detail"]


def test_checkpointing_is_not_wired():
    """Persisted graph state would put applicant data at rest (ADR 0012). The pipeline is
    one synchronous run with nothing to resume, so this must stay None — asserted rather
    than trusted to a default."""
    coordinator, _ = _coordinator([])
    assert coordinator._graph.checkpointer is None


def test_the_maker_is_given_figures_as_exact_strings():
    """A float reaching the prompt would be reformatted somewhere in the chain, and the
    maker would faithfully copy the reformatted value — the gate would then block a
    correct document over two spellings of the same number."""
    coordinator, _ = _coordinator([_document(), _narration()])
    coordinator.run(1)

    rendered = coordinator.client.adapter.calls[0].messages[0]["content"]
    for value in FIGURES.values():
        assert value in rendered
