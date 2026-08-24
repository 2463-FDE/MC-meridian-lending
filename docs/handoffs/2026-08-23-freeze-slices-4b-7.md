# Handoff — freeze slices 4b–7: the loop swap, the gate, the surface, the pack (2026-08-23)

**Branch:** none — slices 1–3 are merged; 4b starts from `main` · **Base:** `main` @ `92fb4fa`
**Repo:** `/Users/maha/Desktop/revature/MC-meridian-lending`
**Status:** on track. Every trace leg the freeze names is now built and **seen on a live
Bedrock run**. One fail condition remains: the framework is in the repo, not in the request
path. Freeze **Wed 2026-09-02**, presentation **Fri 2026-09-04**.

Plan of record: `docs/plan-freeze-agentic-week10.md` §5 (on `main`). This file supersedes its
slice-4 scope and records two decisions taken after it was written.

## What's done

Slices 1–3 merged. Four branches pushed, each `make prove` PROVEN, all with PRs open:

| Work | Branch | PR | Head |
|---|---|---|---|
| Slice 1 demo runtime | `fix/demo-runtime` | #66 merged | — |
| Slice 2 chat-model seam | `feat/agentic-trace-week10` | #67 merged | — |
| Slice 3 root trace | `feat/agentic-root-trace` | #68 merged | — |
| Provider-error leak + identifier clause | `fix/trace-provider-error-leak` | **#70** | `e863197` |
| `policy_topic` channel | `feat/policy-topic-channel` | **#71** | `b4e4212` |
| Slice 4a tool schemas | `feat/native-tool-schemas` | **#72** | `e9970bd` |
| Migration 0018 parse fix | `fix/migration-0018-dollar-quote` | **#73** | `d57649d` |

**The root trace is demonstrated, not asserted.** Live Bedrock, `?policy_topic=debt_to_income`:

```
assistant.request                      task=explain, policy_topic=debt_to_income
  assistant.step (1)
    llm.complete -> llm.transport      real Bedrock us-east-1, 869->48 tok
    tool.search_policy                 status=policy_hit
      policy.retrieval  [retriever]    chunk=underwriting_guidelines#debt-to-income-dti, 0.5303
  assistant.step (2)
    llm.complete -> llm.transport      927->31 tok
    tool.get_decision_record           outcome=refer, status=recorded
  assistant.step (3)
    llm.complete -> llm.transport      1014->83 tok
    assistant.validate                 narration_validated=true
```

Three findings from that run, all fixed on the branches above: the model never invoked
retrieval (no question channel existed, and a free-text one is masked by design);
`POLICY_RETRIEVAL_MIN_SCORE` could not reach a compose stack at all; and a provider 400 put
the provider's own `{'message': ...}` body on the `llm.complete` span via the exception
traceback, on default settings.

## Decisions taken after the plan was written

1. **No identifiers on spans, not even internal surrogate keys** — plan decision 9, rewritten
   in `e863197`. #68's review round (`0cc9575`, `d049f51`) stripped `application_id` and
   `request_id` and codified the CONTENT RULE at `app/assistant.py:44`: enum codes, integers,
   booleans, retrieval scores and chunk ids only. The keep-the-keys position is recorded as a
   rejected alternative with its cost. **Consequence: the demo cannot navigate by
   `request_id`** — slice 6's trace run id replaces that.
2. **The officer's retrieval channel is a closed vocabulary, not a question.** `policy_topic`,
   eight codes, one per retrievable corpus section. A typed question is masked to
   `"•••• (free text redacted)"` before the model sees it, measured twice against live Bedrock.

## What's left

**Slice 4b — the loop swap. The last fail condition.** `MeridianChatModel` is imported by
nothing but `tests/test_chat_model.py`; no `langchain`/`langgraph`/`create_agent` appears in
`app/assistant.py` or `app/main.py`. Until this lands, "disconnected framework" is true.

Start from `feat/native-tool-schemas` (#72) — it is 4b's prerequisite half and adds
`CompletionRequest.tools`, `Completion.tool_calls` and the `_redacted_turn` content-block rule.
Then: swap `run()` to `create_agent` (one new pin, `langchain`), make `bind_tools` real
(`app/llm/chat_model.py:339` currently raises), and invert `tests/test_prompt_contracts.py`,
which asserts the **absence** of `tool_choice`.

Four interlocks to preserve, each owed a test in slice 5:

| # | Interlock | Where on `main` | What breaks |
|---|---|---|---|
| 1 | single-score cache | `app/assistant.py:487` `score_result is None` | two `score_application` calls in one turn = two regulated `decision_events` |
| 2 | explain-path substitution | `app/assistant.py:482` `task == "explain"` | a score on explain is a billable credit pull nobody asked for; `create_agent` binds tools once, so this must move into the tool closure |
| 3 | query strip | `app/assistant.py:523` the `k != "input"` rewrite | the framework owns the message list; **this already failed silently once** — see Blockers |
| 4 | step exhaustion as a refusal | `app/assistant.py:34` `_MAX_STEPS`, mapped at `app/main.py:415` | `create_agent`'s recursion limit raises something else; unmapped that is a 500 where a refusal was intended |

**Slice 5 — blocking `agentic-loop-gate`.** `test_assistant.py`, `test_policy_retrieval.py`,
`test_prompt_contracts.py`, `test_chat_model.py`, `test_assistant_trace.py`,
`test_policy_topic.py`, plus one test per interlock. All of those run only in the `backend`
matrix under `continue-on-error` + `|| true` today, so the whole agentic surface can regress on
a green build. Also owed: an injection suite against the `search_policy` query (the only
model-authored input in the system), and the still-missing test asserting which provider and
region are actually selected at runtime.

**Slice 6 — the trace surface.** `frontend/app/underwriting/[appId]/page.tsx`: render
`policy_citations` / `policy_searches` as a list with their `doc#section` ids, the tool steps
taken, **and the trace run id** (this is the Option-C replacement for the lost `request_id`
navigation — return `get_current_run_tree().trace_id` from the assistant response;
`get_current_run_tree` is already imported at `app/llm/client.py:144`, so no new dependency).
Add a `policy_topic` control wired to the new query parameter. Keep `isForApplication`, the
`routeGen` drop, and `applyRecordedDecision`'s fail-closed rule. `npm run build` is what CI
runs; `npm run lint` is unusable here.

**Slice 7 — freeze pack, no code.** Demo script with a rehearsed failure (`make up` → officer
detail → Explain with a policy topic → citation in the panel → the trace opened from the run
id). Rehearsed failure: unset `POLICY_RETRIEVAL_MIN_SCORE` and show the abstain reaching the
officer as a reason-specific line. Exact-SHA Bedrock proof run off a clean tree with the
credential **exported** (a bare assign is invisible to the child process) and `--out /tmp/...`
because the default path is gitignored. Say **Haiku 4.5** aloud. Then the document truth pass.

## Blockers / open questions

- **None blocking.** All four branches merge clean; #73's conflict with `d204082` is resolved
  in `d57649d` (both test sets kept, migration re-executed against live Postgres, exit 0).
- **Read this before touching interlock 3.** In slice 2 the query strip was reimplemented with
  the tool name in a leading-underscore class attribute. `BaseChatModel` is a pydantic model, so
  that becomes a `ModelPrivateAttr` rather than the string; the comparison never matched and the
  model-authored query rode into history. It was caught **only because the test asserted
  `FakeAdapter.calls` — the outbound `CompletionRequest` — not the seam's intent.** Assert
  outbound requests throughout slice 4b.
- **`LLM_TRACE_CONTENT` must be `false` for the graded run.** It is `true` in the local
  `.env.local` and it puts `system`, `messages`, `text` and `result` on the spans, which the
  requirement forbids outright. `compose-hardening-gate` blocks a *committed* file from enabling
  it; nothing can stop a shell.
- **Two questions still out to the requester**, both with a stated fallback so neither stops
  work: which flow is the demonstration (proceeding on Explain), and whether "add no extra
  agents" caps the total at one (proceeding as *add none*, so the disclosure maker-checker is
  untouched).
- **`docs/debt-log.md` D19 is stale** — still "Open… not built — spec-only week" after #63 and
  #65 merged. Deliberately untouched; needs a pass that describes what is on `main` today.

## Key files

- `services/origination-service/app/assistant.py:34,44,206,482,487,523` — `_MAX_STEPS`, the
  CONTENT RULE, `_TOOLS`, and interlocks 1–3. Slice 4b's centre of gravity.
- `services/origination-service/app/assistant.py:65,184` — `_SPAN_RETRIEVAL` and the retrieval
  span. Re-anchor it after the swap.
- `services/origination-service/app/llm/chat_model.py:339` — `bind_tools`, currently raising.
- `services/origination-service/app/main.py:415` — `_run_assistant`, where refusals map to
  404/422/502/503. Interlock 4 lands here.
- `services/origination-service/app/policy_retrieval.py` — `POLICY_TOPICS`, duplicated into
  `app/llm/request_builder.py::_SAFE_CATEGORICAL` with a parity test (importing it into
  `app/llm/` would drag `rag_eval` into the redaction path).
- `services/origination-service/app/disclosure_coordinator.py` — `tracing_context(enabled=False)`.
  **Read-only. Do not relax it to produce a span.**

## How to verify / run

```bash
cd services/origination-service && python3 -m pytest -q          # 771 passed, 1 xfailed
python3 -m pytest tests/test_llm_client.py tests/test_llm_startup.py tests/test_pii_matrix.py -q
env -u PYTHONPATH python3 -c "import app.main"                   # CI's import smoke
cd ../.. && ./scripts/check_doc_paths.sh && ./scripts/check_doc_claims.sh && ./scripts/spec_diff_gate.sh
./scripts/check_compose_trace_flag.sh && ./scripts/test_check_compose_trace_flag.sh   # 15 passed
make prove REF=<sha>                                             # detached worktree; aborts on dirty tree
```

Live stack: worktree `MC-meridian-lending-worktrees/demo-stack`, branch `demo/agentic-freeze`
(local-only by convention), all slices merged locally. Bring it up with
`set -a; . ./.env.local; set +a; docker compose up -d --force-recreate origination-service` —
compose interpolates from that file only if it is sourced first, and my shell and yours are
different shells.

**Current result:** all of the above green as of this session. The live Bedrock run and the
LangSmith span read-back were done on the demo stack, not on `main` — `main` carries slices
1–3 only until #70–#73 merge.

## Branch state (cite the client's real baseline)

- `main` @ `92fb4fa` = the client's real state. Carries the root trace, the chat-model seam,
  `search_policy` + ADR 0019, the Bedrock region pin, and D19's schema rung **and** capture path
  (#63, #65). Still carries the live debt: ADR 0010 ownership IDOR, CVV retention (D13), float
  money math, no ledger.
- #70–#73 = proposed changes on top, mutually independent.
- `demo/agentic-freeze` = local-only integration branch for the live demo. Never push it.
- Cite `git show main:<file>` for client-current state, never a working branch.

## Debt log refs

- No entries opened or closed. D16 (pgvector deferred) and D28 (equal nested timeouts) belong in
  slice 7's known-limitations list, not fixed.
- D19: schema rung (#63) and capture path (#65) both on `main`; #73 makes migration 0018
  actually executable on an upgraded volume. The debt-log entry still says "not built".
- D3 (lost update) still has no fix code anywhere.

## Next session: start here

Open #72 (`feat/native-tool-schemas`), get it merged, then swap `run()` to `create_agent` on a
branch off `main` — preserving the four interlocks above and asserting the OUTBOUND
`CompletionRequest` in every test, not the seam's intent. That is slice 4b and it is the only
thing standing between this deliverable and every freeze fail condition being closed.
