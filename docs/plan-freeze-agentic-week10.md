# Plan — agentic design and root trace for the 2026-09-02 code freeze

**Written:** 2026-08-23 · **Base:** `main` @ `06c27a3` · **Freeze:** Wed 2026-09-02 ·
**Handoff presentation:** Fri 2026-09-04 · **Working days:** 8 (Aug 24–28, Aug 31–Sep 2)

**Companion documents.** `docs/handoffs/2026-08-21-freeze-scope-response.md` records the
inbound scope and the verified state as of 08-21; `docs/handoffs/2026-08-21-freeze-agentic-trace.md`
is its resume pointer. Both predate PR #64 and state that policy retrieval is absent from
`main`. That is no longer true — see §1. This plan supersedes their "what's left" sections.

---

## 1. What the freeze asks for, and what `main` already satisfies

The program instruction of 2026-08-21 makes the agentic design and its trace the whole
delivery priority until demonstrated, which displaces the payment-integrity work (D19
duplicate charge, D3 lost update) off the critical path. It names five fail conditions: a
direct prompt-to-text call, preloaded retrieval, a disconnected framework, a mock-only path,
and an agent label without real tool invocation.

Three of the runtime requirements landed before this plan begins.

| Requirement | State on `main` @ `06c27a3` | Evidence |
|---|---|---|
| One bounded, read-only policy-document retrieval tool the model invokes alongside the score and memory tools | **Met** | `search_policy` is the third entry in `_TOOLS` (`services/origination-service/app/assistant.py:99`), backed by `app/policy_retrieval.py` over the committed 9-chunk `policies/` corpus. PR #64 |
| Reproducible selection of real Bedrock | **Met, bar the proof run** | `_DEFAULT_BEDROCK_REGION = "us-east-1"` with a Bedrock-runtime region allowlist (`app/llm/config.py:31,184`); `scripts/bedrock_proof.py`. PR #59 |
| Deterministic reason codes, LLM drafts nothing that reaches the officer | **Met** | `_validated_final` rebuilds the officer summary from the persisted record unconditionally (`assistant.py:224`) |
| `LLM_ENABLED` reproducibility for the demo | **Open** | The variable appears in no `.env.example` line and in no compose `environment:` block, so a fresh `make up` returns 503 from the assistant |
| Framework adoption and real tool invocation | **Open** | `tool_choice` appears nowhere in the tree; `tests/test_prompt_contracts.py:4` asserts its absence. The loop parses a JSON action out of response text |
| One privacy-safe root trace | **Open** | Two spans exist: `llm.complete` (`app/llm/client.py:114`) and its `llm.transport` child. Nothing above them |
| A trace surface a reviewer or client can read | **Open** | `policy_citations` is returned by the API (`assistant.py:269`) and read by nothing under `frontend/` |

**Consequence for the schedule.** The remaining work is narrower than the instruction reads:
it is the framework migration, the root trace, the demo runtime, and the handover pack.

## 2. Where each week's work stands on `main`

| Week | Theme | On `main` | Named gap |
|---|---|---|---|
| 1 | LLM client | `ClaudeClient` (30s timeout, 3 retries, schema validation, cost guard), `ModelAdapter` with Claude, Bedrock and fake adapters, `PiiRedactor` in all 7 services behind `redactor-drift` and `redaction-tests` | The Bedrock proof receipt's success path has not been run; it needs a live credential |
| 2 | RAG | The `rag_eval` harness behind the blocking `rag-eval-gate`, and the product retrieval path behind `rag-eval-import-gate`, which proves retrieval inside the built image | `rag-eval-gate` asserts hygiene, cache and PII absence but no retrieval-quality floor. `q11-why-6012-denied` retrieves at 0.338 and is recorded false-confident. In-memory exact cosine; pgvector is D16 |
| 3 | Single agent | `run()` with `_MAX_STEPS = 6`, three tools, the officer-supplied application id, the single-score cache, `_validated_final` | The JSON-action protocol. `test_assistant.py` (25 cases) has no blocking gate |
| 4 | Multi-agent and graph | The LangGraph disclosure pipeline behind `disclosure-lifecycle-gate` (32 cases): six nodes, a deterministic verify gate, one bounded retry edge | `_assemble` and `_narrate` are prompt roles with no tools and no loop. `README.md` and `ARCHITECTURE.md:82` say "single-agent" |
| 5 | Spec-driven development | `spec-diff-gate`: every tracked `adr/*.md` and `docs/spec-*.md` is mapped in `scripts/spec_gate_map.txt` or carries an `# EXEMPT:` reason | Week 5's own subject shipped as a spec package with no `services/` or `db/` file; **superseded** — D19 shipped in Week 9 (PR #63 schema, PR #65 claim path; see `docs/kb.md` Week 9) |
| 6 | AI-augmented SDLC | `make prove` (red without the fix, green with it), the comprehension report, characterization tests, and the red lost-update test | D3 is unfixed and ADR 0014 is Proposed |
| 7 | Observability | D1–D6: the `request_id` span across payment and servicing, the reconciliation report CLI, the variance alert, and the blocking `reconciliation-gate` | The spec states its own reductions: named fields in text log lines, no span or duration semantics, no instrumentation framework, schedulable but not scheduled. The gateway mints no correlation id, so the span starts at payment-service |
| 8 | Security and governance | Reason codes written in the decision's own transaction and carried to both screens, the model card, ADR 0016's export contract, and seven blocking gates | ADR 0010 ownership authorization is open and is live exposure. CVV retention is live (D13) |

One **Explain** request already crosses weeks 1, 2, 3 and 8. The week absent from it is 7:
nothing shows the request happening. That absence is this plan's subject.

## 3. Decisions

1. **Adopt LangChain v1's agent framework, and wrap `ClaudeClient` as a `BaseChatModel`.**
   The framework owns the loop; this service keeps transport, retry, validation and the
   redaction boundary, so `redaction-tests` and `redactor-drift` are unaffected. One new pin
   (`langchain`); `langgraph==1.2.10` and `langchain-core==1.5.3` are already pinned. Using
   the framework's own Bedrock client instead would route around every guard in `app/llm/`
   and require re-proving a blocking gate, which is the opposite of minimizing migration
   risk. The Claude Agent SDK owns the transport as well as the loop, so it is a worse fit
   for the same reason, and it adds a second framework to one image.
2. **LangGraph stays on the disclosure pipeline only.** The instruction's LangGraph clause is
   a constraint on where a graph is justified, not a licence to host the officer loop on one.
   The disclosure pipeline has a typed state and a review transition; the officer loop has
   neither.
3. **Disable parallel tool use, and declare all three tools `strict`.** Parallel tool use is
   on by default and would allow two `score_application` calls to dispatch within one turn.
   Strict declaration makes the provider validate tool input exactly, which is stronger than
   the current post-hoc validator.
4. **Build the root trace from explicit content-free spans, never by enabling the framework's
   tracer.** `disclosure_coordinator.run()` suppresses graph tracing unconditionally because
   a compiled `StateGraph` is a runnable and the tracer would send raw `DisclosureState` to
   LangSmith. That suppression stays, and the new agent does not get a tracer either.
5. **Demonstrate the Explain path.** `_search_policy` refuses on `task == "decision"` by
   design (ADR 0019 decision 5), so the decision path invokes the tool and always receives an
   abstention. Retrieval is real on `GET /assistant/decisions/{app_id}`.
6. **Keep TF-IDF as the retrieval default.** It is keyless and deterministic and reaches
   hit@3 = 1.00 on 9 chunks. The Titan comparison is presented as measured evidence
   (hit@1 0.90 to 0.70, MRR 0.95 to 0.85, unanswerable 1/2 to 2/2), with D16's growth trigger
   named.
7. **Present the disclosure maker-checker as it is, and correct the documents.** No code
   change. `README.md` and `ARCHITECTURE.md` claim a single agent while two agent surfaces
   exist.
8. **Wire the demo into `docker-compose.demo.yml`.** Reproducible demo steps cannot rest on a
   manual `.env` edit.

9. **Retain no identifiers on a span, not even internal surrogate keys.** The requirement
   bans retaining identifiers, and the question is whether "identifiers" means client
   identity or anything that points at a record. We take the literal reading: a span
   carries enum codes, integers, booleans, retrieval scores and chunk ids, and nothing
   else — the CONTENT RULE at `app/assistant.py:44`. Neither `application_id` nor
   `request_id` goes to LangSmith.

   The reason is consistency with a call this codebase already made. `app/llm/client.py`
   and `app/llm/transport.py` strip the caller-supplied `idempotency_key` from their
   spans, and chose omission over hashing because there is no service-owned secret to
   key an HMAC. LangSmith is a third-party sink outside the client boundary, and an
   application id is a stable pointer to a customer record: with vendor access alone,
   someone could enumerate which applications were decisioned, when, and to what outcome,
   with none of the database's authz in front of it. Leaving the root span as the single
   exception made the rule one-span-special, and a bright line survives review where a
   case-by-case exception is re-litigated every round.

   **Rejected alternative, and why it is not unreasonable:** keep both keys on the
   grounds that they are our own surrogates, carry no applicant attribute, and are
   meaningless outside this database — and that dropping them makes the trace unjoinable
   to the record it describes. That is a real cost, and it was weighed. It loses because
   the audit trail was never the trace: `decision_events` is the regulated record, append
   only, authz'd, inside the boundary. The trace is a debugging aid, and a debugging aid
   is the wrong thing to accept vendor-side customer linkability for.

   **What replaces the join**, so the cost is paid rather than ignored: slice 6 returns
   the LangSmith run id in the assistant response, and the officer screen links to it.
   The direction is what matters — our record points at the trace, the trace never points
   at the customer. Someone with vendor access alone learns nothing about which
   application anything belonged to; an officer already authorised for that application
   gets one click. No new secret, no pseudonym, and the bright line holds.

   Two residual costs, stated rather than smoothed over. Correlating a trace to a run now
   goes through the local log, which still carries `app_id` (`app/assistant.py`, the
   narration-contradiction warning) inside the client boundary and through the redacting
   formatter — unambiguous for one officer, ambiguous under concurrent runs until slice 6
   lands. And `d049f51` also stripped `app_id` from `ApplicationNotFound` and from httpx
   errors whose URL embedded it, so an officer-facing 404 no longer names the application;
   that buys nothing once the span is clean, because an HTTP response body never reaches
   LangSmith, and it is worth revisiting on its own.

   Verified on 2026-08-23 by reading every span of a real Bedrock run back from the live
   project: assistant spans carry no `inputs` and no `outputs` at all, and metadata is
   enum codes (`task`, `policy_topic`, `outcome`, `policy_band`, `record_status`,
   `status`, `reason_codes`), counters, booleans, and retrieval's own score and chunk id.

## 4. What the migration does not change, and the four interlocks it does

The regulated output is deterministic today, and it is established below and after the loop
rather than inside it. The decision is computed by `decision-service` and written append-only;
`_validated_final` rebuilds the officer summary from the persisted record without exception;
`_policy_section` renders corpus text verbatim in code and the model never receives that text;
every regulated field in the response is read from the record. The framework sits above all of
it.

Four safety interlocks live in the loop being replaced. Each needs a test in the new blocking
gate.

| Interlock | Location | Failure if lost |
|---|---|---|
| The single-score cache | `assistant.py:340` | It is safe today only because the loop dispatches one action per turn. Two `score_application` calls in one turn would produce two regulated decision events. The `request_id` idempotency key is the second line of defence, since `decision-service` replays on a repeated key rather than appending |
| The Explain-path substitution | `assistant.py:336` | A score request on `task="explain"` is served from the record, because a fresh score is a billable credit pull the officer did not ask for. Tools bind once under the framework, so this must move into the tool closure |
| The query strip | `assistant.py:370` | The model-authored query is removed before the turn enters history. The framework owns the message list, and the tool-call block carrying that query enters it automatically, so this control needs an explicit replacement rather than a port. The code comment states the reason: a boundary that holds only because the redactor catches it is one allowlist entry away from leaking |
| Step exhaustion as a refusal | `assistant.py:325`, mapped at `app/main.py:415` | Exhaustion raises `AssistantError` and maps to 404, 422 or 503. The framework's recursion limit raises a different error; unmapped, a stuck agent returns 500 where a refusal is intended |

One property improves: the model can currently name a tool that does not exist, which raises
`assistant requested unknown tool`. Provider-enforced schemas remove that failure class.

## 5. Slices

Each slice is one pull request, at or under 800 changed lines, with at most two open at a time.

| # | Slice | Day | Content |
|---|---|---|---|
| 1 | Demo runtime and document truth | 1 | `LLM_ENABLED`, `POLICY_RETRIEVAL_MIN_SCORE` and `LANGSMITH_TRACING` into the demo override, credentials host-shell-only; the missing `LLM_ENABLED` block in `.env.example`; the stale `search_policy` "not built" claims corrected in `scripts/check_rag_eval_import.sh`, `.github/workflows/ci.yml` and `docs/cards-week8-governance.md` (which also still credited an unmerged branch). The threshold in `.env.example` was checked against a fresh `python3 -m rag_eval.run` and is correct at 0.1609 — the disagreeing 0.1806 was a stale generated report, not a repository inconsistency |
| 2 | `MeridianChatModel` | 1–2 | A `BaseChatModel` over `ClaudeClient`, plus the three strict tool schemas, and no loop change. This slice exists to prove the redaction boundary survives the framework calling into it |
| 3 | The root trace | 2–4 | Entry, per-step, per-tool, retrieval, validation and outcome spans on the existing loop. Reuses the `@traceable` mechanism at `app/llm/client.py:114`. **Merged as #68 WITHOUT the `request_id` this row originally promised** — its review round stripped both identifiers from the span (`0cc9575`, `d049f51`), which is decision 9 above; the run-to-record join moves to slice 6's trace run id |
| 4 | The loop swap | 5–6 | `run()` becomes a framework agent, preserving the four interlocks and the request-scoped record fetch. Re-anchors the per-step and per-tool spans |
| 5 | Blocking gate and injection resistance | 6–7 | A blocking `agentic-loop-gate` covering the assistant, retrieval, prompt-contract and trace suites plus one test per interlock; an injection suite against the only model-authored input in the system; and the still-missing test asserting which provider and region are actually selected |
| 6 | The trace surface | 7 | The officer assistant card renders `policy_citations` and `policy_searches` as a list with chunk ids, the tool steps taken, and the trace run id |
| 7 | Freeze pack | 7–8 | Demo script with a rehearsed failure, the exact-SHA Bedrock proof run, the real/fixture/fallback statement naming Haiku 4.5, known limitations, and the document truth pass |

## 6. Known limitations to state at handover

- `app/llm/adapter.py:198` uses the legacy `bedrock-runtime` InvokeModel client; current
  practice is the Mantle client. Deliberately unchanged.
- Retrieval uses an in-memory index with exact cosine similarity. pgvector is D16, with a
  corpus-growth trigger.
- Retrieval is refused on the decision path by design, so the decision flow invokes the tool
  and receives an abstention.
- The corpus is 9 chunks from two policy documents.
- `rag-eval-gate` asserts corpus hygiene, cache correctness and the absence of PII, but no
  retrieval-quality floor.
- The week-3 alternatives round recommended a self-hosted trace backend on data-residency
  grounds, and this platform sends spans to LangSmith. The mitigation is the payload
  allowlisting at `app/llm/client.py:39-75` and `app/llm/transport.py:61-140`, which limits
  what leaves the boundary to enumerations, counts, hashes, retrieval scores and chunk
  ids — no identifier of any kind, per decision 9. Two exceptions to state plainly rather than imply, both found by reading a
  live run back on 2026-08-23:
  - `LLM_TRACE_CONTENT=true` adds the prompt (`system`, `messages`), the raw provider
    response (`text`) and the validated body (`result`) to the spans. It defaults to
    false and **must be false for the graded run** — with it on, the trace retains
    prompts and responses, which the requirement forbids outright.
  - The allowlists shape SUCCESSFUL payloads only, so they never covered the error path.
    A provider 400 put a 1606-character traceback carrying the provider's own
    `{'message': ...}` body on the `llm.complete` span. Fixed by raising with
    `from None` at both translation sites in `app/llm/adapter.py`, with
    `tests/test_trace_error_boundary.py` asserting the rendered traceback.
- The trace root opens inside `assistant.run()`, so "entry" means entry to the assistant,
  not the officer's HTTP request. The route wrapper (`app/main.py::_run_assistant`) and the
  gateway hop are not spans, so a 404/422/503 refused before the loop starts produces no
  trace at all.
- The model is Claude Haiku 4.5, not an Opus- or Sonnet-tier model.

## 7. Verification

Each slice runs the origination suite, the assistant, retrieval, prompt-contract, LLM-client
and PII-matrix files, `scripts/check_rag_eval_import.sh` against a real image build,
`scripts/smoke_rag_eval.sh`, `scripts/check_doc_paths.sh`, the frontend build, and `make prove`
from a detached worktree. After slice 4, four checks run explicitly against the live stack:
two score calls in one turn produce exactly one decision event; Explain never reaches
`decision-service /decisions`; the model-authored query appears in no outbound request body
and on no span; and step exhaustion returns the mapped refusal rather than a 500.

## 8. Open items

- **The payment-integrity work is half landed, and it no longer competes for the queue.**
  #63 merged on 2026-08-23 as `06c27a3`, carrying `db/migrations/0018_payments_idempotency.sql`
  — the schema rung that claims an idempotency key in the schema rather than in a service. The
  second fix, the capture-side change, is built and its pull request is not yet raised. **No
  pull request is open**, so the working agreement's cap of two is free and the slices below
  are not queued behind anything.
- **Upside for the presentation.** The idempotency work restores the strongest number
  available: one $100 intent sent eight ways produced eight charges totalling $800 with only
  $600 credited (`scripts/repro_double_charge.py`). The W10 bar asks for a before-and-after
  number said aloud in the first two minutes, and this is it — presented as a second, separate
  result from the agentic deliverable rather than folded into it.
- **Two questions to the requester, both with a stated fallback**, so neither blocks: which
  flow is the demonstration (this plan proceeds on Explain), and whether "add no extra agents"
  caps the total at one (this plan proceeds as "add none").
