# Freeze scope response — 2026-08-21

**Inbound:** program email, 2026-08-21. **Freeze moved: Friday 2026-08-28 → Wednesday 2026-09-02.**
Simulated business-client handoff presentation Friday 2026-09-04. Eight working days
(Aug 24–28, Aug 31–Sep 2).

**Not client correspondence.** This is program/internal and is kept out of the `docs/client-asks-*`
set on purpose — that set is the record of what Dana was asked and what she answered, and mixing an
internal scope reply into it would corrupt an audit surface.

**Priority replaced, not adjusted.** *"Until the agentic design and trace are demonstrated, they are
100% of your delivery priority."* The payment-integrity work (D19 duplicate charge, D3 lost update)
comes off the critical path.

**Consequence nobody should discover later: every client-ask doc still says "code freeze Friday
2026-08-28".** The date is now wrong in all of them, and in the three-question fence to Dana whose
urgency rested on it. Correcting that is its own commit.

---

## Verified state, against each thing the email asserts

Checked against `main` today, not recalled.

| Their claim | Verdict | Evidence |
|---|---|---|
| `LLM_ENABLED` is off by default | **Correct** | `services/origination-service/app/main.py:37` — `os.getenv("LLM_ENABLED", "")`, empty default |
| The enabled provider defaults to direct Anthropic | **Correct** | `app/llm/config.py:25` `_PROVIDERS = ("anthropic", "bedrock")`; `:199` selects the Bedrock model only when the provider is already `bedrock` |
| Region selection is not fixed | **Correct** | `config.py:236` `aws_region=os.getenv("AWS_REGION")`; `:107` *"None lets boto3 resolve it"*; `.env.example:122` has it commented out |
| A conditionally reachable Bedrock adapter exists | **Correct** | `app/llm/adapter.py:175` `BedrockAdapter`, lazily imported; `anthropic[bedrock]==0.116.0` already pinned, so the extra is present rather than missing |
| Substantial agent work already exists | **Correct** | `app/assistant.py` — bounded loop `_MAX_STEPS=6`, score + memory tools, app id taken from the officer's request rather than the model's echo, single-score cache, `_validated_final` checking the answer against the persisted record |
| The remaining runtime gap is reproducibility | **Correct, and tracing is the larger half** | Today there are exactly two spans: `llm.complete` (`app/llm/client.py:101`) and its `llm.transport` child. Nothing above them — no entry, agent-decision, tool, validation or outcome span. Span metadata carries only `validation_failed`, `fallback_used`, `rejection_error` (`client.py:193-197`): no provider field, no execution-mode field |

## Gaps, ranked by how badly they bite

**1. The policy retrieval tool refuses on the decision path, by design.** `_search_policy` on
`feat/policy-retrieval` (PR #57) returns `policy_retrieval.abstain(DECISION_TASK)` when
`task == "decision"` — ADR 0018 decision 5, because the corpus carries Reg B adverse-action guidance
and reason codes are produced deterministically by `decision-service`. So on the decision flow the
model invokes the tool and always gets an abstain: **real invocation, no retrieval.** Against their
failure list (*"preloaded retrieval… agent label without real tool invocation"*) that reads badly
even though it is the correct compliance posture. Either the demo runs `task="explain"`, or ADR 0018
decision 5 changes — and that is a control, so it is their call in writing, not ours.

**2. The single-root-trace requirement collides with an existing control, if they mean the
disclosure agent.** `disclosure_coordinator.run()` wraps its graph invoke in
`tracing_context(enabled=False)` **unconditionally**: a compiled `StateGraph` is a `Runnable`, and
LangChain's callback manager attaches a tracer to any `Runnable.invoke()` once tracing is on, which
would ship raw `DisclosureState` — principal, term, figures, the assembled document — to LangSmith.
Spec D4 requires that suppression. **Resolution taken:** build the root trace from explicit
content-free spans on the officer path rather than by enabling the tracer. No control is undone to
obtain a trace.

**3. Framework adoption lands on the officer assistant, and that is also its own answer.** LangGraph
is already justified where it sits — the disclosure pipeline has genuine state and a review
transition. The assistant is the framework-free one: a hand-rolled JSON-action loop with **no
`tool_choice` anywhere in the build path**, a property `tests/test_prompt_contracts.py` asserts.
Migrating it to native tool calling satisfies the framework requirement and removes the
agent-label-without-real-tool-invocation reading in one change.

## Bedrock — escalated, then resolved the same day

**Resolved 2026-08-21:** a Claude-on-Bedrock key was provided, region **`us-east-1`**, credential
form **`AWS_BEARER_TOKEN_BEDROCK`** (a Bedrock API key / bearer token, not an access-key pair). The
escalation below is kept as the record of what was asked and why.

*Originally escalated:* proving an exact-SHA Bedrock path needs real model access in a pinned
region, granted per account, per model, per region — not ours to grant. The only Bedrock call we had
verified was Titan embeddings in the retrieval harness: different model, separate grant.

### What the answer settles

| Item | Position |
|---|---|
| Region literal | **`us-east-1`**. Already the value in `.env.example:122`; *"us-east"* alone is not a valid region string and reproducible selection depends on the exact literal |
| Model | `us.anthropic.claude-haiku-4-5-20251001-v1:0` (`app/llm/config.py:23`). The `us.` prefix is a **cross-region inference profile** scoped to the US region family, so **`us-east-1` is compatible and no model-ID change is needed** |
| Which model, said out loud | **Haiku 4.5**, not an Opus- or Sonnet-tier model. Name it in the "explicit real/fixture/fallback status" deliverable — a reviewer checking "real Bedrock" should not have to infer the tier |
| Provider selection | `CLAUDE_PROVIDER=bedrock`; `CLAUDE_API_KEY` is ignored on that path |

### Verified working 2026-08-21 — the bearer-token path is proven

**A live Bedrock call succeeded** with `CLAUDE_PROVIDER=bedrock`, `AWS_REGION=us-east-1` and
`AWS_BEARER_TOKEN_BEDROCK`. So `anthropic==0.116.0`'s `AnthropicBedrock` does honour the bearer
token, the credential form does not change, and there is **no Bedrock unknown left**.

Worth recording *why* this was flagged before it was proven: `AWS_BEARER_TOKEN_BEDROCK` appears in
exactly two places in the tree — a docstring at `app/llm/adapter.py:173` and an `.env.example`
comment. **No code reads it and no test covers it.** The adapter builds
`anthropic.AnthropicBedrock(...)`, which resolves credentials through the SDK's own chain, so nothing
in the repo asserted the path — it worked by inheritance rather than by design.

**Two things still follow from that, and neither is a blocker:**

- **The freeze wants an *exact-SHA proof*, not a working call.** A successful run is what unblocks
  the work; a captured, reproducible artifact pinned to a commit is the deliverable. That stays on
  the list.
- **A green local run proves nothing about a clean environment** — the lesson from the httpx
  cross-step defect, where a gate passed locally and died at collection in CI. Since no test covers
  the credential path, CI will not catch a regression in it. Worth one test that asserts the provider
  and region actually selected, rather than trusting the env.

### Legacy vs current Bedrock client — noted, deliberately not changed

`adapter.py:198` uses `anthropic.AnthropicBedrock`, the legacy `bedrock-runtime` InvokeModel path.
Current practice is the Mantle client (`AnthropicBedrockMantle(aws_region=...)`, with plain
`anthropic.`-prefixed model IDs rather than the `us.`-profile form). The legacy path is what our code
already proves out, switching is optional, and polish is explicitly deferred until the core lands —
**so it stays, recorded under known limitations rather than fixed.**

## Answered: "exact reviewed main" means everything from main

**Confirmed 2026-08-21.** All five open pull requests land on `main` before the freeze, and the
deliverable is measured from `main` — not from a feature branch, not from a stacked head. So the
drain is a hard dependency of the freeze, not housekeeping deferred behind the agentic work.

Not required on `main`: the payment-integrity work (D19, D3). It is displaced, and displaced means
it does not gate the freeze.

### Merge topology — checked, and it contains the failure this repo has already had

```
#57  head=feat/policy-retrieval        base=chore/rag-eval-import-seam   <-- stacked, NOT on main
#56  head=chore/rag-eval-import-seam   base=main
#54  head=ci/reconciliation-gate       base=main
#53  head=docs/double-charge-interim   base=main
#52  head=docs/debt-log-d5-status      base=main
```

**Outcome, recorded 2026-08-22: the drain ran and the trap fired anyway.** #52 (`253ea28`),
#53 (`bec5bf3`), #55 (`399fb73`), #54 (`9cfce81`) and #56 (`fb1fc66`) are on `main`. **#57 is not.**
Its base was never retargeted, so it merged into `chore/rag-eval-import-seam` at 22:59:18 — 39
seconds after that branch had already merged to `main` at 22:58:39 — leaving `0cdee4e` on the
branch alone. `main` has no `policy_retrieval` module and no `adr/0019`. The base check below was
the mitigation and it was not performed. Recovery: a fresh PR from `feat/policy-retrieval` rebased
onto `main`, since the stale base branch is now behind `main` by the whole week-7 reconciliation set
and re-merging it would regress `main`.

**#57 is stacked on #56.** #56 must reach `main` first; #57's base then auto-retargets. **This exact
shape already cost this repo once** — #45 and #46 merged into *branches* rather than `main` and
nearly stranded the D4 alert and the D6 runbook. Check #57's base immediately before merging it
rather than trusting the default.

**The ADR collision resolves itself as a consequence.** #57 sits last in the chain, so #53 lands
first and keeps `adr/0018-interim-handling-of-a-double-charged-borrower.md`. **#57 renumbers to
`adr/0019-policy-retrieval-on-the-assistant-loop.md`**, moving its `scripts/spec_gate_map.txt` row
with it. That agrees with the lower-PR-keeps-the-number rule, so there is nothing to arbitrate.

### Merge order

1. **#54** — the only one whose absence lets a defect ship silently. First regardless. *(Done,
   `9cfce81`.)*
2. **#52**, **#53** — both based on `main`. #53 keeps ADR 0018. *(Done, `253ea28` and `bec5bf3`;
   #53 kept the number as predicted.)*
3. **#56** — the `rag_eval` import seam. Load-bearing: CI's backend import smoke does not tolerate
   failures, and an unimportable `rag_eval` makes the policy tool abstain rather than retrieve, which
   their criteria fail as a mock-only path.
4. **#57** — confirm the base retargeted to `main`, renumber the ADR to 0019 with its gate-map row,
   then merge. This is requirement 7 arriving on `main`.

## Secondary risks on the path

The open-PR count and the ADR collision are covered above, now that "everything from main" is
confirmed. What remains:

- **The import seam is load-bearing.** CI's backend import smoke does not tolerate failures, and an
  unimportable `rag_eval` makes the tool abstain rather than retrieve. Their criteria fail a
  mock-only path, so #56 landing correctly is what makes #57 real.

## Question 4 elaborated — "add no extra agents", and why it decides gap 2

**Two agent surfaces exist today, not one.** The officer decision assistant (`app/assistant.py`),
which their email describes approvingly, and the **disclosure maker-checker**
(`app/disclosure_coordinator.py`) — `_assemble` (maker) and `_narrate` (checker) inside a LangGraph
pipeline. Our own documents call that multi-agent: ADR 0012 §D5 *"Multi-agent is maker-checker"*, and
`docs/spec-disclosure-week4.md` §D4 *"Multi-agent disclosure assembly — maker-checker on LangGraph"*.
(`loan_summary` is a single completion, not an agent.)

**Two readings.** (a) a cap on the total — the delivery contains one agent, so the maker-checker is
removed, folded, or kept out of the narrative; (b) an instruction about the migration — do not spawn
new agents while adopting the framework. (b) is the natural reading: the sentence sits beside
*"minimize migration risk"* and a paragraph above *"framework theater"*.

**Why it matters: it decides whether gap 2 is a real conflict.** Requirement 8 wants one root trace
covering *"agent decisions"*. Under (b) that means the officer path and there is no conflict — the
disclosure graph keeps its unconditional tracing suppression. Under (a), if the maker-checker is part
of "the agentic design", the trace has to cover it, which means **undoing a compliance control to
satisfy a tracing requirement.** The collision exists only under reading (a).

**Their own LangGraph clause argues for keeping it.** *"LangGraph only where genuine state or review
transitions justify it"* describes the disclosure pipeline exactly: a typed `DisclosureState`, a
deterministic verify gate that is the only stage able to fail the run, and one bounded cycle keyed on
a typed reason (`render_mismatch` retries; a wrong number never does). Remove it and LangGraph
survives only where it is *not* justified — the inverse of the instruction.

**What (a) would cost.** Refactoring merged code behind two blocking CI jobs (`tila-vectors-gate`,
`disclosure-lifecycle-gate`) inside eight working days, for no functional gain. The cheaper-looking
alternative — leave the code, omit it from the narrative — is worse: concealing real agent work is
framework theater pointed the other way.

**Absent an answer:** reading (b). The disclosure pipeline is untouched, the assistant migrates, the
root trace covers the officer path, and the D4 suppression stays.

## Decisions taken unless overruled

1. **LangChain v1 agent framework over the Claude Agent SDK.** Pins already in the image, LangSmith
   traces LangChain natively, and the SDK would add a second framework plus a weaker path to the
   single root trace. **This reverses our own earlier alt-research recommendation**, on integration
   grounds rather than merit.
2. **Root trace from explicit content-free spans**, not by enabling the LangChain tracer. See gap 2.
3. **The officer assistant moves to native tool calling.** See gap 3.
4. **Demo the explain path** so retrieval is real, pending their answer on gap 1.

## Plan, in dependency order

1. **Drain the five PRs to `main`** — Bedrock is proven, so nothing external gates the work and this
   is now first on the critical path. `#54` first; `#57` renumbers its ADR to `0019`. Order and the
   stacked-base trap above. **Status 2026-08-22: four of five landed; #57 did not and needs a fresh
   PR rebased onto `main`.** Still first on the critical path.
2. Pin the reproducible Bedrock selection — provider default, fixed region, `LLM_ENABLED` handling
   for the demo — and correct the provider and execution-mode metadata.
3. Migrate the officer assistant to native tool calling, preserving the bounded loop, the
   officer-supplied application id, the single-score cache and `_validated_final` unchanged.
4. Build the root trace: entry → agent decisions → policy retrieval → Bedrock → existing tools →
   deterministic validation → business outcome. Existing strippers and content-exclusion tests stay
   green.
5. Injection-resistance suite, a test asserting the provider and region actually selected,
   reproducible demo steps, explicit real/fixture/fallback status,
   source/version/citation provenance, known limitations, handoff ownership, and the exact-SHA proof
   run.

---

## Reply — kept local

The paste-ready reply to the program email is deliberately not published. It lives on the
local-only `docs/cycle-w9-finalise` branch, with the client-ask working set.
