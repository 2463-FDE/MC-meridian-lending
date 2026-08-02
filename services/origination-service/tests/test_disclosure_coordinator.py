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
            # Digit-free: the prose fields carry no figures (see the maker prompt) —
            # a restated amount is both an unchecked second copy and, concatenated
            # with its neighbours, a Luhn-valid run the PII leak guard masks.
            "payment_terms": "Equal monthly payments, due the same day each month.",
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


COMPLETE_CHAIN = {
    "disclosure_id": 5,
    "offer_id": 11,
    "decision_event_id": 7,
    "application_id": 42,
    "applicant_id": 3,
    "chain_complete": True,
    "missing_edges": [],
}


def _coordinator(responses, *, context=None, offer=None, persisted=None, chain=None):
    calls = {"compute": 0, "persist": 0, "provenance": 0}

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

    def read_provenance(disclosure_id):
        calls["provenance"] += 1
        return dict(chain if chain is not None else COMPLETE_CHAIN)

    coordinator = LangGraphDisclosureCoordinator(
        _client(responses),
        gather=lambda app_id: dict(context if context is not None else APPROVED),
        compute_offer=compute_offer,
        persist_disclosure=persist,
        read_provenance=read_provenance,
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
    assert result["provenance"]["chain_complete"] is True
    assert calls == {"compute": 1, "persist": 1, "provenance": 1}


@pytest.mark.parametrize("outcome", ["refer", "deny", "counteroffer", None])
def test_only_an_approved_application_enters_the_pipeline(outcome):
    """Stage 0 exits for everything else — refer goes to manual review, deny to adverse
    action (roadmap), counteroffer cannot be emitted at all."""
    coordinator, calls = _coordinator([], context={**APPROVED, "outcome": outcome})
    result = coordinator.run(1)

    assert result["status"] == "blocked"
    assert result["reason"] == BlockReason.NOT_APPROVED
    assert calls == {
        "compute": 0,
        "persist": 0,
        "provenance": 0,
    }, "must not compute or persist"


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


def test_the_chain_is_read_back_from_the_kg_after_persisting():
    """Spec D3: the pipeline pulls from the KG, it does not merely write to it.

    The read happens after the write because the edge `offers.decision_event_id` is closed
    by the same POST that inserts the disclosure — before stage 5 there is no chain to walk.
    """
    seen = []
    coordinator, calls = _coordinator([_document(), _narration()])
    inner = coordinator._read_provenance

    def spy(disclosure_id):
        seen.append(disclosure_id)
        return inner(disclosure_id)

    # The graph holds bound methods, so the node reads this attribute at call time.
    coordinator._read_provenance = spy
    result = coordinator.run(1)

    assert seen == [5], "the chain must be read back by the persisted disclosure id"
    assert result["provenance"]["applicant_id"] == 3
    assert calls["persist"] == 1


def test_an_incomplete_chain_blocks_and_names_the_missing_edges():
    """A disclosure that cannot be walked back to an applicant is the exact gap ADR 0012
    exists to close, so the run reports it instead of returning ok."""
    coordinator, calls = _coordinator(
        [_document(), _narration()],
        chain={
            **COMPLETE_CHAIN,
            "applicant_id": None,
            "chain_complete": False,
            "missing_edges": ["applicant_id"],
        },
    )
    result = coordinator.run(1)

    assert result["status"] == "blocked"
    assert result["reason"] == BlockReason.PROVENANCE_INCOMPLETE
    assert "applicant_id" in result["detail"]
    assert "disclosure_id=5" in result["detail"]
    assert calls["persist"] == 1


def test_a_stage_five_block_still_hands_back_the_persisted_draft():
    """The row is already the authoritative record of what was computed. Withholding it to
    make the failure look clean would hide the evidence of the broken chain."""
    coordinator, _ = _coordinator(
        [_document(), _narration()],
        chain={
            **COMPLETE_CHAIN,
            "chain_complete": False,
            "missing_edges": ["offer_id"],
        },
    )
    result = coordinator.run(1)

    assert result["disclosure"] == {"disclosure_id": 5, "status": "draft"}
    assert result["provenance"]["missing_edges"] == ["offer_id"]


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


class TestProseFieldsCarryNoDigits:
    """Found by running the maker against a real model on Bedrock, not FakeAdapter.

    Haiku wrote a borrower summary restating four money figures in one sentence. The PII
    leak guard runs the redactor over the model's output, and the redactor's PAN scan is
    deliberately separator-free within a single quoted value — so the concatenated digits
    of "17460.00 ... 3628.71 ... 21088.71 ... 439.35" formed an 18-digit run that passed
    Luhn and was masked as a card number. `guard_output` then rejected the whole document
    and the endpoint returned 503.

    Roughly one in ten such runs is Luhn-valid, so the failure is intermittent: the same
    loan renders on one attempt and fails on the next. Canned FakeAdapter responses never
    produced prose with enough figures to collide.
    """

    def test_the_schema_forbids_digits_in_the_prose_fields(self):
        from app.prompts.disclosure_assemble import OUTPUT_SCHEMA

        props = OUTPUT_SCHEMA["properties"]
        for field in ("payment_terms", "prepayment"):
            assert props[field].get("pattern") == r"^\D*$", field

    def test_a_restated_figure_in_prose_fails_the_document(self):
        """The contract is enforced, not merely requested in the prompt."""
        document = json.dumps(
            {
                "heading": "Truth in Lending Disclosure",
                "figures": dict(FIGURES),
                "payment_terms": "You will make 48 monthly payments of 439.35.",
                "prepayment": "No penalty for early payoff.",
            }
        )
        coordinator, calls = _coordinator([document, _narration()])

        with pytest.raises(Exception) as excinfo:
            coordinator.run(1)
        assert "pattern" in str(excinfo.value)
        assert calls["persist"] == 0, "a rejected document must never be persisted"

    def test_the_exact_output_that_broke_the_live_run_is_masked_as_a_pan(self):
        """The collision itself, pinned, in the shape it actually occurred.

        The PAN scan runs per QUOTED value, which is why the model's JSON is the trigger
        and a bare sentence is not: the quotes delimit one field, and the whole field's
        digits are Luhn-checked as a single run. If the heuristic ever stops globbing
        these, this test says so rather than the digit-free rule quietly becoming cargo
        cult.
        """
        from app.redactor import PiiRedactor

        output = json.dumps(
            {
                "borrower_summary": (
                    "You are borrowing 17460.00. You will pay a finance charge of "
                    "3628.71. The total amount you will pay back is 21088.71."
                )
            }
        )
        assert PiiRedactor.redact(output) != output
        assert "(PAN)" in PiiRedactor.redact(output)

    def test_an_individual_figure_is_not_masked(self):
        """The constraint is about several figures sharing one value, not about digits
        being dangerous — `figures` must survive the same guard untouched."""
        from app.redactor import PiiRedactor

        for value in FIGURES.values():
            assert PiiRedactor.redact(value) == value
