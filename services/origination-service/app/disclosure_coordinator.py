"""Disclosure pipeline coordinator — LangGraph orchestration, deterministic gates.

Stages 0-5 as graph nodes, with exactly one automated cycle: 4a -> 3 on a rendering
mismatch, bounded by an attempt counter.

    [0] route      decision outcome; only `approve` enters the pipeline
    [1] gather     application terms + the decision event that authorises the offer
    [2] compute    disclosure-service derives every figure in Decimal — NO LLM
    [3] assemble   maker agent formats the document from those figures
    [4a] verify    deterministic recompute + comparison — the only thing that can fail it
    [4b] narrate   checker agent briefs the officer on a verdict already reached
    [5] persist    disclosure-service writes the authoritative minor-unit record, then the
                   chain is re-read from the KG view and checked for completeness

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

**The graph is read through the view, not re-joined.** Stage 5 closes the provenance edge
and then asks `v_disclosure_provenance` — the one definition of the chain — whether the walk
disclosure -> offer -> decision_event -> application -> applicant is whole, blocking on
`provenance_incomplete` if it is not. Stage 1 is the one read that does NOT go through the
view, and cannot: it gathers the inputs for a chain link that does not exist yet, so there
is no row to read. Those are two single-table lookups by key, not a join — the thing spec D3
forbids is a second, ad-hoc definition of the traversal, and there is exactly one.

LangGraph is orchestration only (ADR 0012). Checkpointing is deliberately not wired:
persisted graph state would hold applicant data at rest, and this pipeline is a single
synchronous run with nothing to resume.

**The graph invocation itself is untraced, on purpose.** `app/llm/client.py` strips
content from its own `llm.complete` span via `process_inputs`/`process_outputs` — but a
compiled `StateGraph` is a `Runnable`, and LangChain's callback-manager configuration
attaches a `LangChainTracer` to ANY `Runnable.invoke()` the moment `LANGCHAIN_TRACING_V2`
/ `LANGSMITH_TRACING` is set, independent of that decorator. Traced without a guard, each
node's raw `DisclosureState` — principal, term_months, figures, the maker's assembled
document — would ship to LangSmith unredacted. `run()` wraps the invoke in
`langsmith.run_helpers.tracing_context(enabled=False)`, which is checked ahead of the env
var (`langsmith.utils.tracing_is_enabled`) and suppresses the tracer for this call
regardless of the global setting.

`LLM_TRACE_CONTENT` does **not** reach this guard. That flag opens up the `llm.transport`
span — the prompt as sent and the reply as received — which is what a person debugging a
prompt actually wants. Graph state is a different and larger surface: every node's
`DisclosureState`, including figures the borrower's document is built from, on every node
transition. Spec D4 requires this suppression as a control, so it stays unconditional
rather than becoming one env var away from off. If per-node visibility is ever genuinely
needed, that is its own decision with its own flag, not a widening of this one.

**The model calls inside the graph ARE traced** (`_complete`). The suppression above is
context-wide, so it originally swallowed the client's own `llm.complete` / `llm.transport`
spans too — this pipeline emitted nothing at all, not even token count or cost, and a
LangSmith project holding Week 3's `decision_assistant` runs and no disclosure runs looked
like broken tracing config rather than a guard overreaching. `_complete` re-enables tracing
for the duration of each client call, so the LLM spans come back while every node
transition stays suppressed.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langsmith.run_helpers import tracing_context

from . import clients, db
from .llm import ClaudeClient
from .logging_config import get_logger

log = get_logger("origination")

DISCLOSURE_URL = os.getenv("DISCLOSURE_URL", "http://disclosure-service:8005")

# The env vars LangChain/LangSmith read to decide whether tracing is on at all.
_TRACING_ENV = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2")


def _tracing_requested() -> bool:
    """True when the operator turned tracing on, read straight from the environment.

    Read here rather than via `langsmith.utils.tracing_is_enabled()`, which consults the
    context variable first and therefore reports False inside `run()`'s suppression — the
    exact place this is called from. See `_complete`.
    """
    return any(os.getenv(name, "").strip().lower() == "true" for name in _TRACING_ENV)


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

# Decimal places each disclosed figure is rendered to. Money is cents; the APR carries
# THREE, because that is what `disclosures.apr` stores (NUMERIC(9,3)) and what the offer
# row holds. Rendering it to two disclosed 9.58 in the borrower-facing document against
# 9.584 in the authoritative record — one number with two spellings, in a single response.
#
# The gate could not catch it: the truncation happened while building the figures the gate
# compares AGAINST, so both sides were truncated identically and stage 4a saw agreement.
# Found on the first live model run that reached stage 5.
FIGURE_PLACES = {"apr": 3}
_DEFAULT_PLACES = 2


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

# Served when the checker's output fails validation or the leak guard. Stage 4b is
# commentary on a verdict stage 4a already reached, so it must not be able to veto a
# document that passed every deterministic check — before this, an unusable narration
# raised and the route turned it into a 503, discarding a verified disclosure that was one
# stage from being persisted (found live 2026-08-01: the model answered with its own
# invented key names).
#
# `hold_for_compliance` rather than `review_and_send` because the brief is what tells the
# officer what they are looking at. Without one, a human should look before sending.
NARRATION_UNAVAILABLE = {
    "summary": (
        "Narration unavailable for this disclosure. The figures were computed "
        "deterministically and matched the rendered document, and the record was written; "
        "only the officer brief is missing. Read the document before sending."
    ),
    "officer_action": "hold_for_compliance",
}


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
    narration_degraded: bool
    disclosure: dict
    provenance: dict
    attempts: int
    checks_passed: int
    blocked: bool
    reason: str
    detail: str


class DownstreamRefused(RuntimeError):
    """disclosure-service answered with a 4xx: a refusal, not an outage.

    `clients.post()` raises the same `httpx.HTTPStatusError` for "the service is down" and
    "the service looked at this and said no" (a recompute disagreement, a not-found offer).
    The route collapses that exception to a generic 502, which tells the officer the wrong
    thing. `main.py`'s own `_downstream` already makes this distinction for the lifecycle
    proxy; the pipeline's two internal POSTs need the same treatment.
    """

    def __init__(self, status_code: int, detail):
        super().__init__(f"disclosure-service refused ({status_code}): {detail}")
        self.status_code = status_code
        self.detail = detail


def _post_or_raise(base_url: str, path: str, payload: dict) -> dict:
    resp = clients.post_raw(base_url, path, payload)
    if 400 <= resp.status_code < 500:
        try:
            detail = resp.json().get("detail", "disclosure request refused")
        except ValueError:
            detail = "disclosure request refused"
        raise DownstreamRefused(resp.status_code, detail)
    resp.raise_for_status()
    return resp.json()


class ApplicationNotFound(RuntimeError):
    """No such application — a 404, distinct from an application that cannot proceed."""


def gather_disclosure_context(application_id: int) -> dict:
    """Stage 1, production implementation: loan terms and the authorising decision.

    Terms come from the STORED application and the rate from server-side policy — never
    from the caller. `/los/*` is reachable anonymously through the gateway and origination
    forwards the internal-service token downstream, so accepting caller-supplied terms
    would make this a confused deputy able to mint a disclosure for fabricated numbers.
    Same binding `make_offer` already does.

    The outcome is read from `decision_events`, not `decisions`: the latter is a mutable
    current-state pointer, the former is the append-only system of record (ADR 0009), and
    it is the row the disclosure's provenance edge points at. The latest event wins — a
    re-decision supersedes.
    """
    from .routers.offers import POLICY_RATE_PCT

    rows = db.query(
        "SELECT amount, term_months FROM applications WHERE id = %s", (application_id,)
    )
    if not rows:
        raise ApplicationNotFound(f"application {application_id} not found")
    application = rows[0]

    events = db.query(
        "SELECT id, outcome FROM decision_events WHERE app_id = %s "
        "ORDER BY decided_at DESC, id DESC LIMIT 1",
        (application_id,),
    )
    latest = events[0] if events else {}
    return {
        "outcome": (latest.get("outcome") or "").lower(),
        "decision_event_id": latest.get("id"),
        "principal": application["amount"],
        "term_months": application["term_months"],
        "annual_rate": POLICY_RATE_PCT,
    }


def build_coordinator(client: ClaudeClient) -> "LangGraphDisclosureCoordinator":
    """The production wiring: real gather, real downstream service calls."""
    return LangGraphDisclosureCoordinator(client, gather=gather_disclosure_context)


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
        read_provenance: Callable[[int], dict] | None = None,
    ):
        self.client = client
        self._gather = gather
        self._compute_offer = compute_offer or self._default_compute_offer
        self._persist_disclosure = persist_disclosure or self._default_persist
        self._read_provenance = read_provenance or self._default_read_provenance
        self._graph = self._build()

    # ---- stages ---------------------------------------------------------------

    def _route(self, state: DisclosureState) -> dict:
        """Stage 0. Only an approved application produces a disclosure."""
        context = self._gather(state["application_id"])
        # Provenance is checked FIRST and separately. Since the outcome is read from the
        # decision event, a missing event would otherwise surface as "not approved" —
        # true but misleading. "No decision on record" and "decisioned, and the answer
        # was no" are different problems with different fixes, and the officer acting on
        # this needs to know which one they have.
        if not context.get("decision_event_id"):
            return {
                **context,
                **_blocked(BlockReason.PROVENANCE_INCOMPLETE, "no decision event"),
            }
        outcome = context.get("outcome")
        if outcome not in PIPELINE_OUTCOMES:
            return {
                **context,
                **_blocked(BlockReason.NOT_APPROVED, f"outcome={outcome}"),
            }
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
        figures = {
            field: _as_text(
                disclosure.get(field), FIGURE_PLACES.get(field, _DEFAULT_PLACES)
            )
            for field in FIGURE_FIELDS
        }
        if any(value is None for value in figures.values()):
            missing = [k for k, v in figures.items() if v is None]
            return _blocked(BlockReason.NUMBER_WRONG, f"missing={','.join(missing)}")
        return {"figures": figures, "offer_id": offer.get("offer_id")}

    def _complete(self, prompt_name: str, **kwargs):
        """Call the model with LangSmith tracing restored for the duration of the call.

        `run()` suppresses tracing for the whole graph invocation so raw `DisclosureState`
        never reaches LangSmith. That suppression is context-wide, so it also swallowed the
        `llm.complete` / `llm.transport` spans the client creates for its own calls: the
        disclosure pipeline produced NO traces at all — not even token count or cost —
        while the decision-assistant path, which runs no graph, traced normally. The
        symptom was a LangSmith project containing only `decision_assistant` runs, and it
        read like the tracing config was broken rather than like a guard doing its job too
        well.

        Re-enabling here restores those spans without restoring graph state: the context
        manager covers only the client call, and every node transition around it stays
        suppressed. What the restored spans actually carry is still decided by the
        strippers in `app/llm` — metadata only, unless `LLM_TRACE_CONTENT=true`.

        Gated on the operator having asked for tracing. `tracing_context(enabled=True)`
        sets a context variable that is checked AHEAD of the environment, so passing True
        unconditionally would turn tracing on for a deployment that never enabled it, and
        the client would start posting to a LangSmith it has no key for.
        """
        with tracing_context(enabled=_tracing_requested()):
            return self.client.complete(prompt_name, **kwargs)

    def _assemble(self, state: DisclosureState) -> dict:
        """Stage 3. Maker: format the document from figures it is given."""
        document = self._complete(
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
        """Stage 4b. Checker: frame a verdict the gate already reached.

        Cannot fail the run. An unusable brief degrades to `NARRATION_UNAVAILABLE` and the
        pipeline continues to stage 5, because the record — not the prose — is the
        deliverable. `fallback` covers validation and leak-guard rejections only; a
        transport failure or an exhausted token budget still raises, since those say the
        model was never reached and the officer should see an outage.
        """
        narration = self._complete(
            "disclosure_narrate",
            application_id=state["application_id"],
            term_months=state["term_months"],
            note_rate_pct=state["annual_rate"],
            checks_passed=state["checks_passed"],
            fallback=NARRATION_UNAVAILABLE,
        )
        degraded = narration is NARRATION_UNAVAILABLE
        if degraded:
            log.warning(
                "disclosure narration degraded app_id=%s: serving the canned brief",
                state["application_id"],
            )
        return {"narration": dict(narration), "narration_degraded": degraded}

    def _persist(self, state: DisclosureState) -> dict:
        """Stage 5. Write the authoritative record, close the edge, then read the chain back.

        The read-back is the KG read (spec D3): one query on `v_disclosure_provenance`
        asking whether the walk to the applicant is whole. It runs AFTER the write because
        that is the first moment a chain exists — the edge `offers.decision_event_id` is
        closed by the same POST that inserts the disclosure.

        An incomplete chain blocks the run, but the draft row stays. It is already the
        authoritative record of what was computed, and deleting it to make the pipeline
        look clean would destroy the evidence an auditor needs to see the gap. The block
        carries the disclosure id so the officer can find it.
        """
        record = self._persist_disclosure(
            {
                "offer_id": state["offer_id"],
                "decision_event_id": state["decision_event_id"],
                "principal": state["principal"],
                "annual_rate": state["annual_rate"],
                "term_months": state["term_months"],
                # The document goes down with the record it describes. It reached stage 4a's
                # figure gate to get here, and disclosure-service checks it again against its
                # own recomputation before storing — this stage does not get to assert
                # agreement on its own word. Without it the row is written undeliverable
                # rather than delivered blind: `delivered` used to be a flag over content
                # that lived only in this run's HTTP response.
                "document": state.get("document"),
            }
        )
        chain = self._read_provenance(record["disclosure_id"])
        if not chain.get("chain_complete"):
            missing = ",".join(chain.get("missing_edges") or ["unknown"])
            return {
                "disclosure": record,
                "provenance": chain,
                **_blocked(
                    BlockReason.PROVENANCE_INCOMPLETE,
                    f"disclosure_id={record['disclosure_id']} missing={missing}",
                ),
            }
        return {"disclosure": record, "provenance": chain}

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
        return _post_or_raise(DISCLOSURE_URL, "/offers", payload)

    @staticmethod
    def _default_persist(payload: dict) -> dict:
        return _post_or_raise(DISCLOSURE_URL, "/disclosures", payload)

    @staticmethod
    def _default_read_provenance(disclosure_id: int) -> dict:
        resp = clients.get(DISCLOSURE_URL, f"/disclosures/{disclosure_id}/provenance")
        resp.raise_for_status()
        return resp.json()

    # ---- Coordinator -----------------------------------------------------------

    def run(self, application_id: int) -> dict:
        # See module docstring: suppresses per-node LangSmith tracing of raw graph state
        # regardless of the global LANGCHAIN_TRACING_V2 / LANGSMITH_TRACING setting.
        with tracing_context(enabled=False):
            final = self._graph.invoke(
                {"application_id": application_id, "attempts": 0}
            )
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
                # Present only when the block happened at stage 5 — the draft exists and
                # the officer needs a handle on it.
                "disclosure": final.get("disclosure"),
                "provenance": final.get("provenance"),
            }
        return {
            "status": "ok",
            "document": final.get("document"),
            "narration": final.get("narration"),
            # Surfaced so the officer's view can say the brief is canned rather than
            # present it as the model's read of the document.
            "narration_degraded": final.get("narration_degraded", False),
            "disclosure": final.get("disclosure"),
            "provenance": final.get("provenance"),
            "attempts": final.get("attempts", 0),
        }


def _as_text(value: Any, places: int = _DEFAULT_PLACES) -> str | None:
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
        return f"{value:.{places}f}"
    return str(value)
