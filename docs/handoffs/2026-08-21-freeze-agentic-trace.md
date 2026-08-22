# Handoff — agentic design + LangSmith root trace for the 2026-09-02 code freeze (2026-08-21)

**Branch:** `docs/freeze-handoff` (this doc plus the scope response) · **Base for the actual build: `main`**
**Status:** analysis complete and committed, **no build work started**. Not blocked.

**Scope of this handoff: the program code-freeze thread only.** The client-ask / Dana thread is
separate and has its own docs, which stay local-only (`docs/client-asks-*` and its own handoff, on
`docs/cycle-w9-finalise`).
Do not mix them — that set is an audit surface for what the client was asked and answered.

**Read first:** `docs/handoffs/2026-08-21-freeze-scope-response.md` on this branch. It is the source
of truth: verified state, three gaps, and the merge topology. Its paste-ready reply is not published
(kept local on `docs/cycle-w9-finalise`). This file is the
resume pointer, not a replacement for it.

## The ask, in one paragraph

Freeze moved **Friday 2026-08-28 → Wednesday 2026-09-02**; simulated client handoff presentation
**Friday 2026-09-04**. Agentic design and trace are **100% of delivery priority until demonstrated**,
which displaces the payment-integrity work (D19 duplicate charge, D3 lost update) off the critical
path. Required by 09-02: reproducible **real Bedrock** selection, **one bounded read-only policy
retrieval tool the model actually invokes**, **one privacy-safe root trace** spanning entry → agent
decisions → retrieval → Bedrock → tools → validation → outcome, corrected provider/execution-mode
metadata, deterministic safety + injection-resistance tests, reproducible demo steps, provenance,
known limitations, handoff ownership, and proof of the exact-SHA Bedrock path. Adopt **LangChain v1
agents or the Claude Agent SDK**, add no extra agents, keep LangGraph only where state or a review
transition justifies it. Explicit fail conditions: a direct prompt-to-text call, preloaded retrieval,
a disconnected framework, a mock-only path, or an agent label without real tool invocation.

## What's done

- `54ba542` — freeze scope response: every runtime claim in the email re-verified against `main`
  (all six hold), three gaps the email does not name, the escalation, the plan.
- `48d9884` — Bedrock resolved (`us-east-1`, bearer token) and the no-extra-agents question elaborated.
- `9ec3cec` — "everything from main" confirmed, merge topology recorded with its known trap.

**No code has been touched.** Everything above is analysis.

## Decisions already taken (do not re-litigate)

1. **LangChain v1 agent framework**, not the Claude Agent SDK — the pins are already in the image
   (`langgraph==1.2.10`, `langchain-core==1.5.3`), LangSmith traces LangChain natively, and the SDK
   would add a second framework plus a weaker path to the single root trace. **This reverses our own
   earlier alt-research recommendation**, on integration grounds.
2. **The root trace is built from explicit content-free spans**, never by enabling the LangChain
   tracer. See gap 2 below.
3. **The officer assistant moves to native tool calling** as part of the migration.
4. **`main` is the deliverable** — all five open PRs land there first, confirmed with the requester.
5. **The disclosure maker-checker stays untouched** — reading "add no extra agents" as *add none*.

## Three gaps that are not in the email

1. **The policy tool refuses on the decision path, by design.** `_search_policy` returns
   `policy_retrieval.abstain(DECISION_TASK)` when `task == "decision"` (ADR 0018 decision 5 — the
   corpus carries Reg B adverse-action guidance and reason codes are deterministic). So on the
   decision flow the model invokes the tool and always gets an abstain: real invocation, **no
   retrieval**. **Demo `task="explain"`** (`GET /assistant/decisions/{app_id}`) so retrieval is real.
   Asked back to the requester; unanswered.
2. **The single-root-trace requirement collides with a control — but only under one reading.**
   `disclosure_coordinator.run()` wraps its graph invoke in `tracing_context(enabled=False)`
   unconditionally: a compiled `StateGraph` is a `Runnable`, and the tracer would ship raw
   `DisclosureState` (principal, term, figures, the assembled document) to LangSmith. Spec D4
   requires that suppression. Under decision 5 above there is no collision — the root trace covers
   the officer path. **Do not undo that suppression to produce a span.**
3. **Framework adoption lands on the officer assistant**, which today is a hand-rolled JSON-action
   loop with **no `tool_choice` anywhere in the build path** — a property `test_prompt_contracts.py`
   asserts. That is the same shape their criteria call "an agent label without real tool invocation",
   so the migration closes the framework requirement and that objection together.

## What's left, in order

1. **Drain the remaining PRs to `main`** — order and the stacked-base trap in the source-of-truth
   doc. **#52 has since merged** (`253ea28` on `origin/main`), so four are left: `#54`, `#53`, `#56`,
   then `#57`, which renumbers its ADR to `0019` and needs its base re-checked. **This is the first
   thing on the critical path**: a live Bedrock call succeeded on 2026-08-21, so nothing external
   gates the work.
3. **Pin reproducible Bedrock selection** — provider default, region literal, `LLM_ENABLED` handling
   for the demo — and add **provider + execution-mode metadata** to the spans.
4. **Migrate the officer assistant to LangChain v1 with native tool calling.** Preserve exactly:
   the bounded loop (`_MAX_STEPS = 6`), the app id taken from the officer's request rather than the
   model's echo, the single-score cache, and `_validated_final`'s check against the persisted record.
5. **Build the root trace** — entry → agent decisions → policy retrieval → Bedrock → existing tools
   → deterministic validation → business outcome. Keep the existing strippers and content-exclusion
   tests green.
6. **Injection-resistance suite, reproducible demo steps, real/fixture/fallback status, provenance,
   known limitations, handoff ownership, exact-SHA proof run.**

## Blockers / open questions

- **No blockers.** The Bedrock escalation closed the day it was raised, and the credential path was
  then **proven with a live call** — `anthropic==0.116.0`'s `AnthropicBedrock` does honour
  `AWS_BEARER_TOKEN_BEDROCK`.
- **Two things still owed on Bedrock, neither blocking:** the freeze wants an **exact-SHA proof
  artifact**, which a working call is not; and **no test covers the credential path**, so CI cannot
  catch a regression in it — the httpx cross-step defect is the precedent for a green local run
  proving nothing about a clean environment.
- **Two questions out to the requester, both with a stated fallback** so neither stops work:
  which flow is the demo (gap 1 — we proceed on `explain`), and whether "add no extra agents" caps
  the total at one (we proceed as *add none*).

## Key files

- `services/origination-service/app/assistant.py` — the agent that migrates. `_MAX_STEPS = 6`, `_TOOLS`.
- `services/origination-service/app/llm/config.py:23` — `_DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"`. The `us.` prefix is a cross-region inference profile scoped to the US family, so **`us-east-1` needs no ID change**. Note it is **Haiku 4.5**, not an Opus tier — say so in the real/fixture/fallback deliverable.
- `services/origination-service/app/llm/config.py:236` — `aws_region=os.getenv("AWS_REGION")`, unset lets boto3 resolve. This is the "region not fixed" finding.
- `services/origination-service/app/llm/adapter.py:198` — `anthropic.AnthropicBedrock(**kwargs)`, the **legacy `bedrock-runtime` InvokeModel** path. Current practice is the Mantle client with plain `anthropic.`-prefixed ids. Deliberately not changed; goes under known limitations.
- `services/origination-service/app/llm/client.py:101` — the `@traceable` on `llm.complete`. Today's entire trace is this span plus its `llm.transport` child. The root trace hangs above it.
- `services/origination-service/app/main.py:37` — `os.getenv("LLM_ENABLED", "")`, the off-by-default gate.
- `services/origination-service/app/disclosure_coordinator.py` — `run()` holds the `tracing_context(enabled=False)` control. Leave it.
- `services/gateway/app/main.py:421` — `POST /lss/{path:path}` proxied verbatim, authentication only.

## How to verify / run

```bash
cd services/origination-service && python -m pytest -q          # service suite
python -m pytest tests/test_llm_client.py tests/test_prompt_contracts.py -q   # content exclusion + the no-tool_choice assertion
./scripts/check_doc_paths.sh                                     # same invocation CI uses
```

**Current result: not run this session.** Nothing was rebuilt or re-tested — the session produced
analysis and documents only. Run the suites before trusting any inherited green.

## Branch state

- `main` = the client's real state, and the freeze deliverable. Currently missing all five open PRs.
- `docs/freeze-handoff` = this doc and the scope response, based on `origin/main`. Documents only.
- `docs/cycle-w9-finalise` = local-only, carries the client-ask corrections, the unpublished reply,
  and the full copies of both freeze docs. **Do not build the agentic work here** — branch from `main`.
- Cite `git show main:<file>` for client-current state, never the working branch.

## Debt log refs

- **D19 / D3 displaced** off the critical path by this instruction. Designed, unbuilt; D3 has a
  runnable failing test as its before-number (`services/servicing-service/tests/test_lost_update.py`).
- No debt entries opened or closed by this session.

## Next session: start here

Merge **#54** to `main`, then #53, then #56, then #57 with its ADR renumbered to `0019` and its base
re-checked (#52 is already in, `253ea28`). Bedrock is proven, so the drain is what everything else
waits on.
