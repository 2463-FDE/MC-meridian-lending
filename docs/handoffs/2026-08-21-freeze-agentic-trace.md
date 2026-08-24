# Handoff — agentic design + LangSmith root trace for the 2026-09-02 code freeze (2026-08-21)

**Branch:** `docs/freeze-handoff` (this doc plus the scope response) · **Base for the actual build: `main`**
**Status:** analysis complete and committed, **no build work started**.
**Blocked on one thing as of 2026-08-22: PR #57 did not reach `main`** — see "Drain state" below.

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
   `policy_retrieval.abstain(DECISION_TASK)` when `task == "decision"` (ADR 0019 decision 5 — the
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

1. **Recover PR #57 onto `main`** — the drain otherwise completed on 2026-08-22, but #57 merged
   into its stacked base instead of `main` and its content is not on `main`. Details and the
   recovery step in "Drain state" below. **This is the first thing on the critical path**: it is
   freeze requirement 7, and a live Bedrock call succeeded on 2026-08-21, so nothing external
   gates the work.
2. **Pin reproducible Bedrock selection** — provider default, region literal, `LLM_ENABLED` handling
   for the demo — and add **provider + execution-mode metadata** to the spans.
3. **Migrate the officer assistant to LangChain v1 with native tool calling.** Preserve exactly:
   the bounded loop (`_MAX_STEPS = 6`), the app id taken from the officer's request rather than the
   model's echo, the single-score cache, and `_validated_final`'s check against the persisted record.
4. **Build the root trace** — entry → agent decisions → policy retrieval → Bedrock → existing tools
   → deterministic validation → business outcome. Keep the existing strippers and content-exclusion
   tests green.
5. **Injection-resistance suite, reproducible demo steps, real/fixture/fallback status, provenance,
   known limitations, handoff ownership, exact-SHA proof run.**

## Blockers / open questions

- **Bedrock: no external blocker.** The escalation closed the day it was raised, and the credential
  path was then **proven with a live call** — `anthropic==0.116.0`'s `AnthropicBedrock` does honour
  `AWS_BEARER_TOKEN_BEDROCK`.
- **Delivery: blocked on recovering PR #57 to `main`.** Freeze requirement 7 — the bounded
  read-only retrieval tool the model actually invokes — is not on the deliverable branch. See
  "Drain state" below; this is first on the critical path and nothing else in this plan
  substitutes for it.
- **Two things still owed on Bedrock, neither blocking:** the freeze wants an exact-SHA proof
  artifact (defined in `docs/handoffs/2026-08-21-freeze-scope-response.md`, "Exact-SHA proof
  artifact — definition" — not restated here); and **no test covers the credential path**, so CI
  cannot catch a regression in it — the httpx cross-step defect is the precedent for a green local
  run proving nothing about a clean environment.
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
./scripts/check_doc_paths.sh                                     # repo root; same invocation CI uses

cd services/origination-service
python -m pytest -q          # service suite
python -m pytest tests/test_llm_client.py tests/test_prompt_contracts.py -q   # content exclusion + the no-tool_choice assertion
```

**Current result: not run this session.** Nothing was rebuilt or re-tested — the session produced
analysis and documents only. Run the suites before trusting any inherited green.

## Branch state

- `main` = the client's real state, and the freeze deliverable. As of 2026-08-22 it carries #52,
  #53, #54, #55 and #56, but **not #57** — see "Drain state". #58 and #59 are still open.
- `docs/freeze-handoff` = this doc and the scope response, based on `origin/main`. Documents only.
- `docs/cycle-w9-finalise` = local-only, carries the client-ask corrections, the unpublished reply,
  and the full copies of both freeze docs. **Do not build the agentic work here** — branch from `main`.
- Cite `git show main:<file>` for client-current state, never the working branch.

## Debt log refs

- **D19 / D3 displaced** off the critical path by this instruction. Designed, unbuilt; D3 has a
  runnable failing test as its before-number (`services/servicing-service/tests/test_lost_update.py`).
- No debt entries opened or closed by this session.

## Drain state — 2026-08-22

Verified against the public API and `git merge-base --is-ancestor <sha> origin/main`, not recalled.

| PR | Merged | Merge commit | On `main` |
|---|---|---|---|
| #52 | 2026-08-22 21:59 | `253ea28` | yes |
| #53 | 2026-08-22 22:16 | `bec5bf3` | yes |
| #55 | 2026-08-21 12:31 | `399fb73` | yes |
| #54 | 2026-08-22 22:58:23 | `9cfce81` | yes |
| #56 | 2026-08-22 22:58:39 | `fb1fc66` | yes |
| **#57** | 2026-08-22 22:59:18 | `0cdee4e` | **no** |

Two PRs opened after this plan was written and are still open: **#58** (`docs/client-qa-register`,
+140/-0) and **#59** (`feat/bedrock-pin`, +592/-2, which is plan item 2 below).

**#57's base was never retargeted, and the stacked-base trap fired.** It merged into
`chore/rag-eval-import-seam` at 22:59:18 — **39 seconds after** that branch had itself merged to
`main` at 22:58:39. So `0cdee4e` lives only on `origin/chore/rag-eval-import-seam`: `main` carries
no `policy_retrieval` module and no `adr/0019`, which means **freeze requirement 7 — the bounded
read-only retrieval tool the model actually invokes — is not on the deliverable branch.** This is
the third occurrence of the same shape; #45 and #46 did it in week 7.

Recovery is not a re-merge of the stale base branch: `origin/chore/rag-eval-import-seam` is behind
`main` by the whole week-7 reconciliation set, so merging it now would regress `main`. Open a fresh
PR from `feat/policy-retrieval` **rebased onto `main`**, confirm the base reads `main` in the API
immediately before merging, and check that `adr/0019-policy-retrieval-on-the-assistant-loop.md` and
its `scripts/spec_gate_map.txt` row land with it.

## Next session: start here

Recover **#57** onto `main` as above — it is freeze requirement 7 and nothing else in the plan
substitutes for it. Then #59 (the Bedrock pin, plan item 2) and #58. Bedrock itself is proven, so
the recovery is what everything else waits on.
