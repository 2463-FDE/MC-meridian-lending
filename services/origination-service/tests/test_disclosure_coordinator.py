"""Disclosure pipeline: routing, the bounded cycle, and the gate that can actually fail.

Runs the real compiled LangGraph on FakeAdapter — no network, no database, no tokens. The
service calls are injected, so what is exercised is the graph itself: which nodes run, in
what order, and what the conditional edges decide.
"""

import json

import pytest

from app.disclosure_coordinator import (
    FIGURE_PLACES,
    MAX_ASSEMBLE_ATTEMPTS,
    BlockReason,
    LangGraphDisclosureCoordinator,
    _as_text,
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


def _coordinator(
    responses, *, context=None, offer=None, persisted=None, chain=None, record=None
):
    calls = {"compute": 0, "persist": 0, "provenance": 0}

    def compute_offer(payload):
        calls["compute"] += 1
        if record is not None:
            record["compute"] = payload
        return (
            offer
            if offer is not None
            else {"offer_id": 11, "disclosure": dict(FIGURES)}
        )

    def persist(payload):
        calls["persist"] += 1
        # Pass a dict as `record` to inspect what stage 5 actually sent downstream.
        if record is not None:
            record["persist"] = payload
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


def test_the_offer_is_created_against_the_decision_that_authorizes_it():
    """Stage 2 sends the decision event down with the offer, so the edge is recorded at
    creation rather than inferred at disclosure time."""
    record = {}
    coordinator, _ = _coordinator([_document(), _narration()], record=record)
    assert coordinator.run(1)["status"] == "ok"
    assert record["compute"]["decision_event_id"] == 7


def test_the_disclosure_cites_the_offers_own_decision_event_not_the_latest():
    """Stage 0 reads the LATEST decision event; the offer may have been authorized by an
    earlier one.

    `create_offer` is idempotent per application and `uq_offers_app` makes the offer
    permanent, so after a re-decision this run replays an offer written under the older
    event. Citing stage 0's newer event would name a decision that did not produce these
    terms, and the provenance view — which joins the decision through the offer — would
    report that chain complete. disclosure-service refuses the mismatch outright, so
    sending the wrong one also blocks the run.
    """
    record = {}
    coordinator, _ = _coordinator(
        [_document(), _narration()],
        offer={"offer_id": 11, "decision_event_id": 3, "disclosure": dict(FIGURES)},
        record=record,
    )
    assert coordinator.run(1)["status"] == "ok"
    assert record["persist"]["decision_event_id"] == 3


def test_a_legacy_offer_without_an_edge_falls_back_to_the_gathered_event():
    """An offer written before the edge existed carries none; the run still proceeds and
    disclosure-service closes it the old way (same-application only)."""
    record = {}
    coordinator, _ = _coordinator(
        [_document(), _narration()],
        offer={"offer_id": 11, "decision_event_id": None, "disclosure": dict(FIGURES)},
        record=record,
    )
    assert coordinator.run(1)["status"] == "ok"
    assert record["persist"]["decision_event_id"] == 7


def test_the_document_is_persisted_with_the_record_it_describes():
    """Stage 5 sends the document down with the row.

    Without this the assembled document lived only in this run's HTTP response, so the row
    carried no evidence of what was disclosed and `delivered` became a flag over content no
    later session could read. disclosure-service re-checks the figures before storing it —
    this stage does not get to assert agreement on its own word.
    """
    record = {}
    coordinator, _ = _coordinator([_document(), _narration()], record=record)
    result = coordinator.run(1)

    assert result["status"] == "ok"
    sent = record["persist"]
    assert sent["document"]["figures"] == FIGURES
    assert sent["document"]["heading"] == "Truth in Lending Disclosure"
    # Still inputs-only otherwise: the numbers the service derives are not sent to it.
    assert set(sent) == {
        "offer_id",
        "decision_event_id",
        "principal",
        "annual_rate",
        "term_months",
        "document",
    }


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


def test_a_rejected_narration_still_persists_the_verified_record():
    """Stage 4b cannot fail the run — found live, 2026-08-01.

    The real model answered the checker prompt with its own invented key names, the
    validator rejected the completion, and the pipeline 503'd — discarding a document whose
    figures had already passed the deterministic gate at 4a. Narration is commentary on a
    verdict already reached; it must not be able to veto the record.

    The canned brief chooses `hold_for_compliance`, not `review_and_send`: an unexplained
    document is one a human should look at before sending.
    """
    unusable = json.dumps({"action": "review_and_send", "key_terms": "48 months"})
    coordinator, calls = _coordinator([_document(), unusable])
    result = coordinator.run(1)

    assert result["status"] == "ok"
    assert result["narration_degraded"] is True
    assert result["narration"]["officer_action"] == "hold_for_compliance"
    assert "unavailable" in result["narration"]["summary"].lower()
    assert calls == {"compute": 1, "persist": 1, "provenance": 1}


def test_a_healthy_narration_is_not_flagged_as_degraded():
    """The degraded flag is the UI's signal, so a good run must not raise it."""
    coordinator, _ = _coordinator([_document(), _narration()])
    result = coordinator.run(1)

    assert result["narration_degraded"] is False
    assert result["narration"]["officer_action"] == "review_and_send"


def _fabricated_narration() -> str:
    return json.dumps(
        {
            "summary": "Your monthly payment of $340.00 is due on the first of the month.",
            "officer_action": "review_and_send",
        }
    )


def test_a_fabricated_dollar_figure_in_the_summary_is_rejected():
    """D1 groundedness guard (spec: disclosure-narration-judge.md).

    Stage 4b is given none of the five disclosed figures — only `term_months` and
    `note_rate_pct` (here 48 and 7.99). A `summary` stating a dollar amount is
    schema-valid and leak-guard-passing, so nothing before this guard catches it.
    It must degrade to NARRATION_UNAVAILABLE via the same path validation/leak-guard
    rejections already use, not reach the officer verbatim.
    """
    coordinator, calls = _coordinator([_document(), _fabricated_narration()])
    result = coordinator.run(1)

    assert result["status"] == "ok"
    assert result["narration_degraded"] is True
    assert result["narration"]["officer_action"] == "hold_for_compliance"
    assert "unavailable" in result["narration"]["summary"].lower()
    assert calls == {"compute": 1, "persist": 1, "provenance": 1}


def _narration_saying(summary: str) -> str:
    return json.dumps({"summary": summary, "officer_action": "review_and_send"})


def test_a_dollar_figure_equal_to_the_term_count_is_still_rejected():
    """D1's allowed set is unit-aware, not value-only.

    `_narrate` is given `term_months` 48 and `note_rate_pct` 7.99 and no money at all, so
    "$48.00" is a fabricated dollar amount even though 48 is one of the two numbers the
    model was handed. A value-only comparison passes it — exactly the figure the guard
    exists to catch.
    """
    coordinator, _ = _coordinator(
        [_document(), _narration_saying("Monthly payment of $48.00 begins next month.")]
    )
    result = coordinator.run(1)

    assert result["narration_degraded"] is True
    assert "unavailable" in result["narration"]["summary"].lower()


def test_a_percent_figure_equal_to_the_term_count_is_rejected():
    """Same unit-awareness on the rate side: 48 is the term, never the rate.

    The only percent this stage may state is `note_rate_pct` (7.99). "48% APR" is a
    fabricated rate whose value happens to equal the term count.
    """
    coordinator, _ = _coordinator(
        [_document(), _narration_saying("The loan carries a 48% APR over the term.")]
    )
    result = coordinator.run(1)

    assert result["narration_degraded"] is True
    assert "unavailable" in result["narration"]["summary"].lower()


def test_a_spelled_out_note_rate_is_not_flagged_as_ungrounded():
    """A truthful spelled-out rate must not degrade the brief.

    "seven point nine nine percent" is `note_rate_pct` written in words. Summing the
    number words instead of reading the decimal yields a value that can never equal 7.99,
    so a correct narration would degrade to NARRATION_UNAVAILABLE on the money path.
    """
    coordinator, _ = _coordinator(
        [
            _document(),
            _narration_saying("The note rate is seven point nine nine percent."),
        ]
    )
    result = coordinator.run(1)

    assert result["narration_degraded"] is False
    assert result["narration"]["officer_action"] == "review_and_send"


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


def test_graph_invocation_suppresses_langsmith_tracing(monkeypatch):
    """A compiled StateGraph is a plain LangChain `Runnable`. Invoking it without a guard
    lets LangChain's callback-manager auto-attach a `LangChainTracer` to it the moment
    LANGCHAIN_TRACING_V2 / LANGSMITH_TRACING is set — independent of the `process_inputs`
    /`process_outputs` stripping `app/llm/client.py` applies to its own span — and ship
    each node's raw `DisclosureState` (principal, figures, the maker's assembled document)
    to LangSmith unredacted. `run()` must suppress tracing for the graph invocation
    regardless of the global setting; this asserts it actually does, the same way
    `test_checkpointing_is_not_wired` asserts the checkpointer is off rather than trusting
    a default."""
    import contextlib

    from app import disclosure_coordinator as mod

    real_tracing_context = mod.tracing_context
    seen = []

    @contextlib.contextmanager
    def spy(*, enabled=None, **kwargs):
        seen.append(enabled)
        with real_tracing_context(enabled=enabled, **kwargs):
            yield

    monkeypatch.setattr(mod, "tracing_context", spy)

    coordinator, _ = _coordinator([_document(), _narration()])
    coordinator.run(1)

    assert False in seen, "run() never suppressed tracing for the graph invocation"


def test_graph_suppression_is_not_relaxed_by_the_trace_content_flag(monkeypatch):
    """`LLM_TRACE_CONTENT=true` opens up the `llm.transport` span — the prompt as sent and
    the reply as received. It must NOT reach this guard. Graph state is a different and
    much larger surface: every node's `DisclosureState`, on every transition, including
    the figures the borrower's document is built from. Spec D4 requires the suppression as
    a control, so it stays unconditional rather than one env var away from off. Without
    this test, a later "make the flag consistent" change would silently widen it."""
    import contextlib

    from app import disclosure_coordinator as mod

    monkeypatch.setenv("LLM_TRACE_CONTENT", "true")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    real_tracing_context = mod.tracing_context
    seen = []

    @contextlib.contextmanager
    def spy(*, enabled=None, **kwargs):
        seen.append(enabled)
        # Always delegate SUPPRESSED, whatever was requested: the assertion is about what
        # the coordinator asks for, and letting enabled=True through would make the suite
        # attempt a real LangSmith ingest (401s in CI, exports in a keyed environment).
        with real_tracing_context(enabled=False, **kwargs):
            yield

    monkeypatch.setattr(mod, "tracing_context", spy)

    coordinator, _ = _coordinator([_document(), _narration()])
    coordinator.run(1)

    # The GRAPH invocation is the outermost entry and must be suppressed. The model calls
    # nested inside it re-enable on purpose (see _complete) — that is not this guard.
    assert seen and seen[0] is False, (
        f"graph tracing was enabled with LLM_TRACE_CONTENT=true (saw {seen})"
    )


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
        # heading is on the same footing: borrower-facing text outside the figures check.
        for field in ("heading", "payment_terms", "prepayment"):
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

    def test_a_number_in_the_heading_fails_the_document(self):
        """The heading is outside the figures check too — a stale number in the title must
        be rejected by the same digit-free contract, not persisted and delivered."""
        document = json.dumps(
            {
                "heading": "Truth in Lending Disclosure 9.58%",
                "figures": dict(FIGURES),
                "payment_terms": "Equal monthly payments until the loan is repaid.",
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


class TestFigureRendering:
    """One number, one spelling. Found on the first live run that reached stage 5."""

    def test_the_apr_reaches_the_maker_with_three_decimals(self):
        """The offer's APR is a float; rendering it to two decimals disclosed 9.58 in the
        document against 9.584 in the authoritative record — in the same response.

        Stage 4a could not catch it, because the truncation happened while building the
        figures the gate compares AGAINST: both sides were truncated identically, so the
        gate saw agreement and passed on the first attempt.
        """
        coordinator, _ = _coordinator(
            [_document(), _narration()],
            offer={
                "offer_id": 11,
                "disclosure": {
                    "apr": 9.584,
                    "finance_charge": 3628.71,
                    "amount_financed": 17460.0,
                    "total_of_payments": 21088.71,
                    "monthly_payment": 439.35,
                },
            },
        )
        rendered = coordinator._compute(
            {
                "application_id": 1,
                "principal": 18000.0,
                "annual_rate": 7.99,
                "term_months": 48,
            }
        )["figures"]

        assert rendered["apr"] == "9.584", "the APR must not lose its third decimal"

    def test_money_still_renders_to_cents(self):
        assert _as_text(439.3, FIGURE_PLACES.get("monthly_payment", 2)) == "439.30"
        assert _as_text(21088.7, 2) == "21088.70"

    def test_a_string_figure_is_passed_through_untouched(self):
        """disclosure-service may hand back an exact decimal string; reformatting it would
        reintroduce the drift this helper exists to prevent."""
        assert _as_text("9.584", 2) == "9.584"


def test_llm_calls_are_traced_even_though_graph_state_is_not(monkeypatch):
    """The graph suppression is context-wide, so before `_complete` existed it also
    swallowed the client's own llm.complete / llm.transport spans — the disclosure pipeline
    produced no traces at all, not even token count or cost. Assert the model call runs
    with tracing RE-ENABLED while the graph invocation around it stays suppressed."""
    import contextlib

    from app import disclosure_coordinator as mod

    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    real_tracing_context = mod.tracing_context
    events = []

    @contextlib.contextmanager
    def spy(*, enabled=None, **kwargs):
        events.append(("enter", enabled))
        # Delegate suppressed regardless — see the note in the flag test above.
        with real_tracing_context(enabled=False, **kwargs):
            yield
        events.append(("exit", enabled))

    monkeypatch.setattr(mod, "tracing_context", spy)

    coordinator, _ = _coordinator([_document(), _narration()])
    coordinator.run(1)

    # The graph invoke is suppressed...
    assert ("enter", False) in events
    # ...and each model call re-enables inside it (assemble + narrate).
    assert events.count(("enter", True)) == 2, events
    # Ordering: the re-enable happens INSIDE the suppression, never replacing it.
    first_false = events.index(("enter", False))
    assert all(i > first_false for i, e in enumerate(events) if e == ("enter", True)), (
        events
    )


def test_llm_tracing_not_forced_on_when_operator_did_not_enable_it(monkeypatch):
    """`tracing_context(enabled=True)` sets a context variable checked AHEAD of the
    environment, so re-enabling unconditionally would turn tracing on for a deployment that
    never asked for it — and the client would start posting to a LangSmith it holds no key
    for. Gate on the env var the operator actually sets."""
    import contextlib

    from app import disclosure_coordinator as mod

    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)

    real_tracing_context = mod.tracing_context
    seen = []

    @contextlib.contextmanager
    def spy(*, enabled=None, **kwargs):
        seen.append(enabled)
        with real_tracing_context(enabled=False, **kwargs):
            yield

    monkeypatch.setattr(mod, "tracing_context", spy)

    coordinator, _ = _coordinator([_document(), _narration()])
    coordinator.run(1)

    assert True not in seen, f"tracing was force-enabled with no env var set: {seen}"


@pytest.mark.parametrize(
    "env,expected",
    [
        ({"LANGSMITH_TRACING": "true"}, True),
        ({"LANGCHAIN_TRACING_V2": "true"}, True),
        ({"LANGSMITH_TRACING": "TRUE"}, True),
        ({"LANGSMITH_TRACING": "false"}, False),
        ({"LANGSMITH_TRACING": "1"}, False),
        ({}, False),
    ],
)
def test_tracing_requested_reads_either_env_var(monkeypatch, env, expected):
    from app import disclosure_coordinator as mod

    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert mod._tracing_requested() is expected
