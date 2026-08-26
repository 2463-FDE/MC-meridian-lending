# ADR 0021: The Officer Loop Runs on a Framework Agent, and Its Trace Is Built by Hand

- **Status:** **Accepted** — built and merged to `main`: the chat-model wrapper and the loop swap
  (PR #76), the root trace (PR #68), the blocking `agentic-loop-gate` (PR #79), and the officer
  trace surface (PR #80). **The root spans the officer loop, not the whole request the program
  instruction asks for** (Context, below): it opens inside `assistant.run()` after the
  `policy_topic` check, so a refusal raised before the loop starts is not a trace, and the
  route's exception mapping in `_run_assistant` sits outside the span. Moving the root to the
  route funnel is consequence 6 under Consequences, open at this ADR's date — cite this ADR for
  the loop's trace, not for the request's.
- **Date:** 2026-08-25
- **Author:** Claude Code
- **Related:** ADR 0005 (LLM client — the transport, retry and redaction boundary this decision
  keeps), ADR 0006 (logging redaction), ADR 0009 §5 (the officer assistant this replaces the loop
  of), ADR 0012 (externalized rule config), ADR 0019 (policy retrieval — the third tool the
  framework binds). Plan: `docs/plan-freeze-agentic-week10.md` §3–§4. Demo and handover:
  `docs/freeze-demo-script-week10.md`.
- **Source:** the 2026-08-21 program instruction for the 2026-09-02 freeze, and the pins read at
  `services/origination-service/requirements.txt`.

---

## Context

The officer assistant answers two questions for a loan officer: decision this application, and
explain the decision on record. Until PR #76 it ran a loop this repository wrote: the prompt
asked the model to emit a JSON action, `run()` parsed that JSON out of the response text,
dispatched a tool, appended the result, and repeated up to six times.

That loop worked and was well tested, and it had one structural weakness the freeze names
directly: the model could name a tool that does not exist, or emit prose where an action
belonged, and both failures were caught after the fact by our own parser rather than by the
provider. A JSON-action protocol carried in response text is also the shape the freeze
instruction rules out — "a direct prompt-to-text call ... does not pass."

The program instruction requires a framework agent with real tool invocation, one bounded
read-only policy-retrieval tool the model chooses to call, and one privacy-safe root trace over
the whole request. It also requires that migration risk be minimized and that no extra agents
be added. Three constraints shape any answer.

**The redaction boundary is the platform's PII control, and it lives below the loop.**
`app/llm/client.py`, `app/llm/transport.py` and `app/llm/request_builder.py` hold the export
contract: every history turn's content must be a JSON object, free text is masked wholesale, and
only allowlisted categorical values survive. Two blocking CI jobs (`redaction-tests`,
`redactor-drift`) hold it. A framework that owns transport routes around all of it.

**The regulated output is deterministic and is established outside the loop.** The decision is
computed by `decision-service` and written append-only; `_validated_final` rebuilds the officer
summary from the persisted record unconditionally; `_policy_section` renders corpus text
verbatim in code. Whatever hosts the loop, the answer an officer reads is record-derived.

**A second framework in one image is a cost, not a feature.** `langgraph==1.2.10` and
`langchain-core==1.5.3` are already pinned, because the disclosure pipeline
(`app/disclosure_coordinator.py`) is a `StateGraph` behind the blocking
`disclosure-lifecycle-gate`. Any pin change is re-proving that gate.

---

## Decision

**1. The officer loop runs on `create_react_agent` from `langgraph.prebuilt`, over a
`BaseChatModel` wrapper around our own client.** `MeridianChatModel` (`app/llm/chat_model.py`)
adapts `ClaudeClient` to the framework's chat-model interface, so the framework owns the loop
while this service keeps transport, retry, schema validation, the cost guard and the redaction
boundary. The provider now enforces tool schemas, which removes the unknown-tool failure class
entirely.

**2. LangGraph hosts the disclosure pipeline as a graph, and the officer loop only as a prebuilt
agent.** The instruction's LangGraph clause constrains where a *graph* is justified. The
disclosure pipeline has a typed state and a review transition and keeps its `StateGraph`. The
officer loop has neither, so it gets no hand-built graph — it gets the framework's own agent
implementation, which is what `create_react_agent` is.

**3. The five safety interlocks move into the tool closures, not into the loop body.**
`create_react_agent` binds tools once, so a check in a loop body would have nowhere to run. The
closures take no application id at all, which is stronger than the loop was: the officer's id is
not *preferred over* the model's, the model has no way to name one.

| Interlock | Failure it prevents |
|---|---|
| Single score per run | Two `score_application` calls would append two regulated `decision_events` |
| Explain-path substitution | A score on a read-only turn is a billable bureau pull nobody asked for |
| Query strip | The model-authored query must not ride into persisted history |
| Step exhaustion as a refusal | A stuck agent must return a refusal, not a 500 |
| Single search per run | Compounding retrieval calls and citations within one run |

**4. Parallel tool use stays disabled, and the tools are NOT declared `strict`.** The adapter
sends `tool_choice: {"type": "auto", "disable_parallel_tool_use": true}`. Disabling parallel use
is load-bearing: the interlocks above assume one action per turn. `strict: true` is available on
the Messages API (top-level on the tool definition, with `additionalProperties: false`), and this
ADR declines it — see the options below.

**5. The root trace is built from explicit content-free spans, and the framework's tracer stays
off.** A compiled graph is a runnable, so a framework tracer would export the state it is handed
— which carries the model's prose and the model-authored query. `harden_trace_client()` claims
the LangSmith singleton at boot, before the client exists, and `disclosure_coordinator.run()`
keeps its unconditional suppression. Spans are emitted by hand: the loop root (Status names its
scope), one per tool dispatch, one for retrieval, one for the deterministic validation, plus the
existing `llm.complete` / `llm.transport` pair per model call.

**6. A span carries enum codes, integers, booleans, retrieval scores and chunk ids, and nothing
else — including no identifiers of our own.** `application_id` and `request_id` are absent from
every span. The officer-facing replacement is the LangSmith run id, returned on the response and
rendered on the officer screen, so our record points at the trace and the trace never points at
the customer.

### Options considered

**A. `create_react_agent` from `langgraph.prebuilt` — chosen.** Real provider-enforced tool
calling, no new dependency, and the redaction boundary untouched. Cost: the symbol is deprecated
(below).

**B. LangChain v1's `create_agent` (`langchain.agents`) — rejected, with reservations.** This is
the instruction's literal wording and the successor to option A. It requires adding the
`langchain` package, which resolves `langchain-core` and `langgraph` to newer versions than the
two pins the disclosure `StateGraph` runs on. Re-proving `disclosure-lifecycle-gate` (32 cases
over a regulated TILA artifact) inside the freeze window is the opposite of minimizing migration
risk. This rejection has a shelf life — see Risks.

**C. The Claude Agent SDK — rejected.** It owns the transport as well as the loop, so the
retry policy, the cost guard, the schema validation and the redaction boundary in `app/llm/`
would all be bypassed, and both blocking redaction jobs would be asserting a path the product no
longer takes. It also adds a second agent framework to an image that already ships LangGraph.

**D. Keep the hand-rolled JSON-action loop — rejected.** It is a prompt-to-text call with a
parser, which the instruction names as a fail condition, and it keeps the unknown-tool and
prose-instead-of-action failure classes that provider-enforced schemas delete.

**E. Declare the three tools `strict: true` — rejected.** `strict` makes the provider validate
tool input exactly, which sounds strictly better than a post-hoc validator. It buys nothing here
and costs something real. Two of the three tools take no arguments at all (`_NoArgs`), and the
closures ignore anything the model sends: a tool call carrying `{"application_id": 99}` validates
to `{}` and runs against the officer's application regardless. So the input shape the model
controls has no effect to validate. Against that, `strict` turns a recoverable model slip into a
refused request: a rejected call comes back as an error turn whose content is prose, and
`_redacted_turn` fails closed on a tool_result that is not a JSON object, so the run raises
`LLMError` and the officer gets a 503. `search_policy`'s one argument is already bounded by
`_PolicyQuery` (`max_length` read from the prompt's own output schema, 200 characters), which is
where that validation belongs.

---

## Consequences

### Positive

- The model cannot name a tool that does not exist; the provider rejects it before dispatch.
- The interlocks are enforced where the framework cannot reach around them — inside the closures
  — rather than in a loop body the framework replaced.
- The trace answers "what did the agent do" for one officer request: the tools it chose, the
  retrieval it ran and what came back, whether the record contradicted the narration, and the
  business outcome.
- No new dependency, so no blocking gate needed re-proving.
- The redaction boundary is unchanged and is now exercised by the framework calling into it.

### Negative / trade-off (accepted)

- **`create_react_agent` is deprecated.** Every run emits
  `LangGraphDeprecatedSinceV10: create_react_agent has been moved to 'langchain.agents' ...
  Deprecated in LangGraph V1.0 to be removed in V2.0`. We ship on a symbol the framework tells
  us to leave, in exchange for not moving two pins under a regulated gate during a freeze.
- **The trace is hand-maintained.** Every new span and every new metadata key is a decision
  someone has to make correctly. A framework tracer would be free and would leak.
- **A trace cannot be joined to a record from the vendor side.** That is deliberate (decision 6)
  and it costs debuggability: correlating a run now goes through the local log and the
  officer-visible run id.
- **Exhaustion has two shapes and both must be handled.** langgraph's soft stop appends
  `AIMessage("Sorry, need more steps to process this request.")` and returns normally; the hard
  stop raises `GraphRecursionError`. Handling only one hands framework prose to an officer as an
  answer.

### Neutral

- `_RECURSION_LIMIT` is `2 * _MAX_STEPS` because langgraph counts node executions and one
  round-trip is two nodes. Six model calls, unchanged from the old loop.
- Retrieval is still refused on the decision path (ADR 0019 decision 5), so the decision flow
  invokes the tool and receives an abstention.

---

## Cross-cutting concerns

**Security.** The redaction boundary is the control and it did not move. The framework's tracer
is off and the LangSmith singleton is primed before the client exists, so no code path can turn
graph state into a span. `LLM_TRACE_CONTENT` remains false by default and no committed compose
file can enable it (`compose-hardening-gate`); a shell can, which is a stated handover
limitation.

**Performance.** One extra object per model call (the chat-model wrapper) and the framework's own
node overhead; the dominant cost is unchanged, the provider round-trip. `trace()` is a no-op
unless `LANGSMITH_TRACING` is set.

**Scalability.** Unchanged and still a known weakness: origination blocks on downstream HTTP with
one module-level 30s timeout equal to the bureau pull's own (debt D28).

**Reliability.** Exhaustion maps to a refusal (404/409/502/503), never an unmapped 500. The
provider's schema enforcement removes one failure class; the framework's error prose introduces
one, and it fails closed at the redaction boundary rather than reaching the officer.

**Maintainability.** The deprecated symbol is the debt this ADR creates, and it is bounded: the
call site is one function, `_build_agent`.

**Cost.** The token budget and cost guard are still in `ClaudeClient`. The single-score interlock
is what keeps a billable bureau pull from firing twice; the model is Claude Haiku 4.5.

**Operational impact.** `CLAUDE_PROVIDER` and `AWS_REGION` are pinned in
`docker-compose.demo.yml`; credentials stay host-shell-only. `LANGSMITH_TRACING` is off by
default, so the trace is opt-in per environment.

**Testing impact.** The blocking `agentic-loop-gate` runs the loop, the interlocks, the trace
content rule, the prompt contracts, the chat model, native tool_use, the tool schemas, retrieval
and the policy-topic vocabulary. `test_prompt_contracts.py` inverted: it asserted `tool_choice`
was ABSENT under the JSON-action protocol and now asserts it is present.

---

## Implementation plan

1. `MeridianChatModel` over `ClaudeClient`, plus the three tool schemas, no loop change — the
   slice that proves the redaction boundary survives the framework calling into it. **Done, #76.**
2. Root trace on the existing loop: request root, per-tool, retrieval, validation, outcome.
   **Done, #68** — and its own review round stripped both identifiers from the span, which is
   decision 6.
3. The loop swap: `run()` becomes a framework agent, preserving the five interlocks and the
   request-scoped record fetch. **Done, #76.**
4. Blocking `agentic-loop-gate`, one test per interlock. **Done, #79.**
5. Officer trace surface: citations, searches, tool steps, run id. **Done, #80.**
6. Move the trace root to the route funnel so a refusal raised before the loop starts is still a
   trace, and translate each caught exception to an enum inside the span rather than raising
   through it. Lands with the trace-entry change; not on `main` at this ADR's date.
7. Prove the exact-SHA Bedrock path and read the correlated trace back. Open.

## Rollback strategy

Reverting decisions 1–3 means reverting #76: `run()` returns to the JSON-action loop, the
interlocks return to the loop body, and `test_prompt_contracts.py` returns to asserting
`tool_choice` is absent. Nothing outside `app/assistant.py` and `app/llm/chat_model.py` changes,
because the framework never owned transport, persistence or validation — which is the property
that makes this reversible at all. The trace (decisions 5–6) is independent: the spans hang off
whatever loop is present, and removing them removes spans, not behaviour. No migration, no data
change, no gate to re-prove in either direction.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| `create_react_agent` is removed in LangGraph V2 | The call site is one function. The move is `langchain.agents.create_agent` plus the `langchain` pin, done deliberately outside a freeze window, with `disclosure-lifecycle-gate` re-proven on the new `langchain-core` |
| Someone enables the framework tracer for debugging and ships graph state to LangSmith | `harden_trace_client()` runs unconditionally at boot; the disclosure suppression is unconditional; `test_assistant_trace.py` asserts what spans may carry |
| A new span or metadata key leaks prose or an identifier | The CONTENT RULE is stated at the span definitions, and the trace tests assert absence (no credit score, no applicant content, no reason prose, no identifiers) under the blocking gate |
| The framework changes its exhaustion behaviour | Both shapes are handled and both are tested; a change makes a test fail rather than an officer read framework prose |
| `strict` is reconsidered without re-reading option E | The rejection is recorded with its mechanism (error turn → prose → fail closed → 503), not as a preference |

## Assumptions challenged

**"Adopting a framework removes code."** It did not. The interlocks moved from the loop body into
the tool closures and the step accounting became the framework's, but nothing was deleted — and
one control (the query strip) needed an explicit replacement rather than a port, because the
framework owns the message list and the tool-call block carrying the query enters it
automatically.

**"Provider-enforced schemas make the post-hoc validator redundant."** Only for the shape of the
tool call. `_validated_final` validates the model's *claims* against the persisted record, which
no schema can do.

**"The instruction's LangGraph clause forbids hosting the loop on LangGraph."** Read literally it
constrains where a graph is justified. Hosting the loop on the framework's own prebuilt agent is
not building a graph for it; building a `StateGraph` for a loop with no typed state would be.

**"A trace needs an id that joins it to the record."** It reads as obvious and it loses: the
regulated audit trail is `decision_events` — append-only, authorized, inside the boundary. The
trace is a debugging aid, and a debugging aid is the wrong thing to accept vendor-side customer
linkability for.

## Sign-off status

Accepted for the 2026-09-02 freeze. Decision 4's `strict` rejection and decision 6's
no-identifiers rule are the two most likely to be re-litigated; both carry their mechanism above
so the next reader argues with the reason rather than the conclusion.
