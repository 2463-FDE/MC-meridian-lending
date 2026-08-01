"""Disclosure pipeline coordinator — LangGraph orchestration, deterministic gates.

Stages 0-5 as graph nodes, with exactly one automated cycle: 4a -> 3 on a rendering
mismatch, bounded by an attempt counter.

    [0] route      decision outcome; only `approve` enters the pipeline
    [1] gather     application terms + the decision event that authorises the offer
    [2] compute    disclosure-service derives every figure in Decimal — NO LLM
    [3] assemble   maker agent formats the document from those figures
    [4a] verify    deterministic recompute + comparison — the only thing that can fail it
    [4b] narrate   checker agent briefs the officer on a verdict already reached
    [5] persist    disclosure-service writes the authoritative minor-unit record

Three properties are worth stating because they are what make an LLM safe on this path:

**The LLM never computes a regulated number.** Stage 2 is a service call; stage 4a is plain
Python. The maker is handed formatted strings and asked to copy them; the checker is given
no figures at all.

**The gate is not the agent.** A model asked "is this correct?" can be wrong permissively,
and a wrong pass on a disclosure is unrecoverable once it reaches the borrower. Stage 4a
compares strings and refuses; 4b only runs after it passes.

**Failures route by typed reason, never by position.** Only `render_mismatch` — the
document text drifted while the numbers are sound — loops back to the maker. Every other
reason blocks. A loop around a deterministic gate would retry until the model produced
something the gate happened to accept, which is how you launder a wrong number into a
document.

LangGraph is orchestration only (ADR 0012). Checkpointing is deliberately not wired:
persisted graph state would hold applicant data at rest, and this pipeline is a single
synchronous run with nothing to resume.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from . import clients
from .llm import ClaudeClient
from .logging_config import get_logger

log = get_logger("origination")

DISCLOSURE_URL = os.getenv("DISCLOSURE_URL", "http://disclosure-service:8005")

# Bounded: the maker gets this many attempts at rendering before the run blocks. Two,
# because a third attempt at the same prompt has never been the difference between a
# correct document and a wrong one — it is just cost.
MAX_ASSEMBLE_ATTEMPTS = 2

# Outcomes that may produce a disclosure. `refer` and `deny` exit; `counteroffer` is
# unreachable today (the bands emit approve/refer/deny) and is not routed.
PIPELINE_OUTCOMES = frozenset({"approve"})

FIGURE_FIELDS = (
    "apr",
    "finance_charge",
    "amount_financed",
    "total_of_payments",
    "monthly_payment",
)


class BlockReason:
    """Typed failure reasons. Only RENDER_MISMATCH is retryable — see module docstring."""

    RENDER_MISMATCH = "render_mismatch"
    NUMBER_WRONG = "number_wrong"
    TOLERANCE_BREACH = "tolerance_breach"
    PROVENANCE_INCOMPLETE = "provenance_incomplete"
    FEE_RATE_INCONSISTENT = "fee_rate_inconsistent"
    RETRIES_EXHAUSTED = "retries_exhausted"
    NOT_APPROVED = "not_approved"


RETRYABLE = frozenset({BlockReason.RENDER_MISMATCH})


class Coordinator(Protocol):
    """The seam that makes the framework reversible (ADR 0012).

    LangGraph is adopted for orchestration; a native implementation of this same protocol
    would be a ~40-line loop. Only the LangGraph implementation is built — a second,
    unexercised implementation would be untested code pretending to be a fallback. What
    the protocol buys is that swapping it touches one class, not the call sites.
    """

    def run(self, application_id: int) -> dict: ...


class DisclosureState(TypedDict, total=False):
    application_id: int
    outcome: str
    decision_event_id: int
    principal: float
    annual_rate: float
    term_months: int
    # Declared because LangGraph filters node output against this schema — a key returned
    # by a node but absent here is silently dropped, and the next stage sees a KeyError.
    offer_id: int
    figures: dict
    document: dict
    narration: dict
    disclosure: dict
    attempts: int
    checks_passed: int
    blocked: bool
    reason: str
    detail: str


def _blocked(reason: str, detail: str = "") -> dict:
    log.warning("disclosure pipeline blocked reason=%s detail=%s", reason, detail)
    return {"blocked": True, "reason": reason, "detail": detail}


class LangGraphDisclosureCoordinator:
    """Stages 0-5 as a compiled LangGraph.

    `gather` and the two service calls are injected so tests exercise the real graph —
    routing, the cycle, the gate — without HTTP or a database. The LLM client is injected
    the same way the decisioning assistant does it, so tests run on FakeAdapter.
    """

    def __init__(
        self,
        client: ClaudeClient,
        *,
        gather: Callable[[int], dict],
        compute_offer: Callable[[dict], dict] | None = None,
        persist_disclosure: Callable[[dict], dict] | None = None,
    ):
        self.client = client
        self._gather = gather
        self._compute_offer = compute_offer or self._default_compute_offer
        self._persist_disclosure = persist_disclosure or self._default_persist
        self._graph = self._build()

    # ---- stages ---------------------------------------------------------------

    def _route(self, state: DisclosureState) -> dict:
        """Stage 0. Only an approved application produces a disclosure."""
        context = self._gather(state["application_id"])
        outcome = context.get("outcome")
        if outcome not in PIPELINE_OUTCOMES:
            return {
                **context,
                **_blocked(BlockReason.NOT_APPROVED, f"outcome={outcome}"),
            }
        if not context.get("decision_event_id"):
            # No provenance edge means the disclosure could not be traced back to the
            # decision that authorised it — the gap ADR 0012 exists to close.
            return {**context, **_blocked(BlockReason.PROVENANCE_INCOMPLETE)}
        return {**context, "attempts": 0, "blocked": False}

    def _compute(self, state: DisclosureState) -> dict:
        """Stage 2. disclosure-service derives every figure in Decimal. No LLM."""
        offer = self._compute_offer(
            {
                "application_id": state["application_id"],
                "principal": state["principal"],
                "annual_rate": state["annual_rate"],
                "term_months": state["term_months"],
            }
        )
        disclosure = offer.get("disclosure", offer)
        figures = {field: _as_text(disclosure.get(field)) for field in FIGURE_FIELDS}
        if any(value is None for value in figures.values()):
            missing = [k for k, v in figures.items() if v is None]
            return _blocked(BlockReason.NUMBER_WRONG, f"missing={','.join(missing)}")
        return {"figures": figures, "offer_id": offer.get("offer_id")}

    def _assemble(self, state: DisclosureState) -> dict:
        """Stage 3. Maker: format the document from figures it is given."""
        document = self.client.complete(
            "disclosure_assemble",
            **state["figures"],
            term_months=state["term_months"],
            note_rate_pct=state["annual_rate"],
        )
        return {"document": document, "attempts": state.get("attempts", 0) + 1}

    def _verify(self, state: DisclosureState) -> dict:
        """Stage 4a. The gate. Deterministic; the only stage that can fail the run."""
        rendered = (state.get("document") or {}).get("figures") or {}
        expected = state["figures"]

        mismatched = [
            field for field in FIGURE_FIELDS if rendered.get(field) != expected[field]
        ]
        if mismatched:
            return _blocked(
                BlockReason.RENDER_MISMATCH,
                f"fields={','.join(mismatched)}",
            )
        return {"blocked": False, "reason": "", "checks_passed": len(FIGURE_FIELDS)}

    def _narrate(self, state: DisclosureState) -> dict:
        """Stage 4b. Checker: frame a verdict the gate already reached."""
        narration = self.client.complete(
            "disclosure_narrate",
            application_id=state["application_id"],
            term_months=state["term_months"],
            note_rate_pct=state["annual_rate"],
            checks_passed=state["checks_passed"],
        )
        return {"narration": narration}

    def _persist(self, state: DisclosureState) -> dict:
        """Stage 5. Write the authoritative record and close the provenance edge."""
        record = self._persist_disclosure(
            {
                "offer_id": state["offer_id"],
                "decision_event_id": state["decision_event_id"],
                "principal": state["principal"],
                "annual_rate": state["annual_rate"],
                "term_months": state["term_months"],
            }
        )
        return {"disclosure": record}

    # ---- edges ----------------------------------------------------------------

    def _after_route(self, state: DisclosureState) -> str:
        return END if state.get("blocked") else "compute"

    def _after_compute(self, state: DisclosureState) -> str:
        return END if state.get("blocked") else "assemble"

    def _after_verify(self, state: DisclosureState) -> str:
        """The one cycle in the graph, and the reason it is safe.

        Only a rendering mismatch retries, and only while attempts remain. A wrong NUMBER
        never loops — retrying it would mean asking the model repeatedly until the gate
        happened to accept something.
        """
        if not state.get("blocked"):
            return "narrate"
        if (
            state.get("reason") in RETRYABLE
            and state.get("attempts", 0) < MAX_ASSEMBLE_ATTEMPTS
        ):
            return "assemble"
        return END

    def _build(self):
        graph = StateGraph(DisclosureState)
        graph.add_node("route", self._route)
        graph.add_node("compute", self._compute)
        graph.add_node("assemble", self._assemble)
        graph.add_node("verify", self._verify)
        graph.add_node("narrate", self._narrate)
        graph.add_node("persist", self._persist)

        graph.add_edge(START, "route")
        graph.add_conditional_edges(
            "route", self._after_route, {"compute": "compute", END: END}
        )
        graph.add_conditional_edges(
            "compute", self._after_compute, {"assemble": "assemble", END: END}
        )
        graph.add_edge("assemble", "verify")
        graph.add_conditional_edges(
            "verify",
            self._after_verify,
            {"assemble": "assemble", "narrate": "narrate", END: END},
        )
        graph.add_edge("narrate", "persist")
        graph.add_edge("persist", END)
        # No checkpointer: see module docstring.
        return graph.compile()

    # ---- default service calls -------------------------------------------------

    @staticmethod
    def _default_compute_offer(payload: dict) -> dict:
        return clients.post(DISCLOSURE_URL, "/offers", payload)

    @staticmethod
    def _default_persist(payload: dict) -> dict:
        return clients.post(DISCLOSURE_URL, "/disclosures", payload)

    # ---- Coordinator -----------------------------------------------------------

    def run(self, application_id: int) -> dict:
        final = self._graph.invoke({"application_id": application_id, "attempts": 0})
        if final.get("blocked"):
            reason = final.get("reason")
            if reason in RETRYABLE:
                # Exhausted the bounded retry: report that, not the last symptom, so the
                # operator sees "we gave up" rather than "the text drifted once".
                reason = BlockReason.RETRIES_EXHAUSTED
            return {
                "status": "blocked",
                "reason": reason,
                "detail": final.get("detail", ""),
                "attempts": final.get("attempts", 0),
            }
        return {
            "status": "ok",
            "document": final.get("document"),
            "narration": final.get("narration"),
            "disclosure": final.get("disclosure"),
            "attempts": final.get("attempts", 0),
        }


def _as_text(value: Any) -> str | None:
    """Figures cross into the prompt as exact strings — never floats.

    A float here would be reformatted by str() at some point in the chain and the maker
    would faithfully copy the reformatted value, so the gate would compare two spellings
    of the same number and block a correct document.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)
