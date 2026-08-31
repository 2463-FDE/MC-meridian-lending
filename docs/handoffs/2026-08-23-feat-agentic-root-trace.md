# Handoff — freeze slices 4–7: native tool calling, the gate, the panel, the pack (2026-08-23)

**Branch:** `feat/agentic-root-trace` · **Base:** `main` @ `06c27a3` · **Repo:** `/Users/maha/Desktop/revature/MC-meridian-lending`
**Status:** on track. Slices 1–3 are built and green; slice 4 is the hard one and its scope
grew during slice 2. Freeze **Wed 2026-09-02**, presentation **Fri 2026-09-04**.

Plan of record: `docs/plans/freeze-agentic-week10.md` (on `fix/demo-runtime`, PR #66 — not
yet on `main`). Read it after this file for the week-by-week position and the framework
reasoning; this file is the resume pointer and supersedes its slice-4 scope.

## What's done

Three slices, three branches, all cut from `main` and mutually independent (no shared
files; `git merge-tree` clean in any order).

| Slice | Branch | PR | Commits | Size |
|---|---|---|---|---|
| 1 — demo runtime + doc truth | `fix/demo-runtime` | **#66 open** | `cfdadc6`, `a87e369` | +257/−7 |
| 2 — the framework seam | `feat/agentic-trace-week10` | **#67 open** | `1ab4415`, `c8b0cbc` | +618 |
| 3 — the root trace | `feat/agentic-root-trace` | **unpushed** | `cfa8a5f` | +556/−65 |

- **Slice 1** — `LLM_ENABLED` was the only variable the LLM feature needs that
  `docker-compose.yml` did not interpolate, so the documented host-env workflow supplied
  the credential and still left every LLM route on 503. Now `${LLM_ENABLED:-}`, empty
  default (enabling it with no credential aborts startup, so a `"true"` default would
  break the whole stack). Also retired three stale "search_policy is not built" claims.
- **Slice 2** — `MeridianChatModel`, a `BaseChatModel` over `ClaudeClient`, so the
  framework calls *down* into `app/llm/` rather than past it. **Zero new dependencies**:
  `langchain-core==1.5.3` was already pinned and carries `BaseChatModel` + `bind_tools`.
- **Slice 3** — one `assistant.request` root per officer request, with `assistant.step`
  per turn, `tool.<name>` per dispatch, `policy.retrieval` inside the policy tool, and
  `assistant.validate` around the record check. `run()` was rewritten rather than patched
  so the tree nests; an AST comparison of its calls shows **nothing removed**.

## What's left

**Slice 4 — native tool calling. This is the one with real risk, and it grew.**

The approved plan assumed adding a `tools=` parameter. Slice 2 proved that is not enough:

- `CompletionRequest` (`app/llm/adapter.py:37`) has **no `tools` field**, and its
  `messages: list[dict]` (`:41`) is flat `{role, content: str}` with no `tool_use` /
  `tool_result` content blocks.
- `_redacted_turn` (`app/llm/request_builder.py:436`) **fails closed** on any turn whose
  content is not a JSON object — deliberately, ADR 0005 least-privilege.
- `_SAFE_CATEGORICAL` (`app/llm/request_builder.py:59`) **is** the assistant protocol
  vocabulary. Any string outside it leaves as a free-text mask.

So slice 4 must (a) carry tool schemas and tool-content blocks through
`build_request` (`app/llm/request_builder.py:507`) into `CompletionRequest`, and (b) give
`_redacted_turn` an explicit, tested rule for tool-call and tool-result blocks. **Do not
loosen the JSON-object rule to get the agent's messages through — that rule is the
control.** This is week-1 code behind two blocking jobs (`redaction-tests`,
`redactor-drift`); budget for re-proving both.

Then swap `run()` to `create_agent` (needs the one new pin, `langchain`), set
`disable_parallel_tool_use` and `strict: True` on all three tools, and invert
`tests/test_prompt_contracts.py` (1 `tool_choice` reference today, asserting **absence**).

**Preserve these four interlocks — each needs its own test in slice 5's gate:**

| # | Interlock | Where | What breaks if lost |
|---|---|---|---|
| 1 | single-score cache | `app/assistant.py`, `score_result is None` | Safe today only because the loop dispatches one action per turn. Two `score_application` calls in one turn ⇒ two regulated `decision_events`. `request_id` idempotency is the second line of defence |
| 2 | Explain-path substitution | same file, `task == "explain"` branch | A score request on explain is served from the record, because a fresh score is a billable credit pull the officer never asked for. `create_agent` binds tools once, so this must move into the tool closure |
| 3 | query strip | same file, the `k != "input"` rewrite | The framework owns the message list, so nothing else will strip the model-authored query. **Already reimplemented once in slice 2 and it failed silently** — see Blockers |
| 4 | step exhaustion as a refusal | `_MAX_STEPS`, mapped at `app/main.py:415` | `create_agent`'s recursion limit raises something else; unmapped that is a 500 where a refusal was intended |

**Slice 5 — blocking `agentic-loop-gate`.** `test_assistant.py`, `test_policy_retrieval.py`,
`test_prompt_contracts.py`, `test_chat_model.py`, `test_assistant_trace.py`, plus one test
per interlock. **All five of those files currently run only in the `backend` matrix under
`continue-on-error` + `|| true`** — the whole agentic surface can regress on a green build.
Copy `reconciliation-gate`'s comment convention and prove the gate by breaking an assertion
on a scratch branch. Also owed here: an injection suite against the `search_policy` query
(the only model-authored input in the system), and the still-missing test asserting which
provider and region are *actually* selected at runtime.

**Slice 6 — the trace surface.** `frontend/app/underwriting/[appId]/page.tsx` only. Extend
`AssistantResult` to read `policy_citations` and `policy_searches` and render citations as
a list with their `doc#section` ids; today they reach the screen only glued into `summary`
prose, and `globals.css` has no `white-space: pre-wrap`, so the separators collapse into
one run-on paragraph. Keep `isForApplication`, the `routeGen` drop, and
`applyRecordedDecision`'s fail-closed rule. `npm run build` is what CI runs; `npm run lint`
is unusable here.

**Slice 7 — freeze pack, no code.** Demo script with a rehearsed failure (`make up` →
officer detail → **Explain** → citation in the panel → same `request_id` opens the root
trace in LangSmith project `2463-FDE`; rehearsed failure = unset the threshold and show the
abstain reaching the officer as a reason-specific line). Exact-SHA Bedrock proof run off a
clean tree with the credential **exported** (a bare assign is invisible to the child
process), `--out /tmp/...` because the default path is gitignored — the paste is the
artifact. Say **Haiku 4.5** aloud. Then the document truth pass: both
`docs/handoffs/2026-08-21-*.md` still say #57 is not on `main` (PR #64 fixed that);
`README.md` and `ARCHITECTURE.md:82` still say "single-agent"; `docs/kb.md` says "All
seventeen" ADRs while `adr/` holds 19, so a fresh session picks a taken number.

## Blockers / open questions

- **None blocking.** All three slices are green and independent.
- **Two questions still out to the requester, both with a stated fallback** so neither
  stops work: which flow is the demonstration (proceeding on Explain, because
  `_search_policy` refuses on `task == "decision"` by design, ADR 0019 decision 5), and
  whether "add no extra agents" caps the total at one (proceeding as *add none*, so the
  disclosure maker-checker is untouched).
- **WIP is over the cap.** Three PRs open (#65, #66, #67) against a stated limit of two, so
  slice 3 is committed but unpushed. Merge something before opening a fourth.
- **Read this before touching interlock 3.** In slice 2 the query strip was reimplemented
  with the tool name in a class attribute. `BaseChatModel` is a pydantic model, so a
  leading-underscore class attribute becomes a `ModelPrivateAttr` rather than the string;
  the comparison never matched and the model-authored query rode into history. The redactor
  masked it, which is the defence in depth `app/assistant.py`'s own strip comment describes.
  **It was caught only because the test asserts `FakeAdapter.calls` — the outbound
  `CompletionRequest` — rather than the seam's intent.** Assert outbound requests in slice 4.

## Key files

- `services/origination-service/app/llm/adapter.py:37,41` — `CompletionRequest`: no `tools`, no content blocks. Slice 4 starts here.
- `services/origination-service/app/llm/request_builder.py:59,436,507` — the allowlist, the fail-closed turn rule, the assembler.
- `services/origination-service/app/llm/chat_model.py` — the seam; `bind_tools` currently **raises** rather than dropping schemas silently.
- `services/origination-service/app/assistant.py` — the loop, the four interlocks, and slice 3's spans.
- `services/origination-service/app/main.py:415` — `_run_assistant`, where refusals map to 404/422/502/503.
- `services/origination-service/app/disclosure_coordinator.py:552` — `tracing_context(enabled=False)`. **Read-only. Do not relax it to produce a span.**

## How to verify / run

```bash
# from a worktree off origin/main — never the chronically dirty main tree
cd services/origination-service && python3 -m pytest -q
python3 -m pytest tests/test_llm_client.py tests/test_llm_startup.py tests/test_pii_matrix.py -q  # blocking redaction-tests
env -u PYTHONPATH python3 -c "import app.main"        # CI's import smoke, bare interpreter
cd ../.. && ./scripts/check_rag_eval_import.sh        # real image build; proves retrieval in-image
./scripts/check_doc_paths.sh && ./scripts/spec_diff_gate.sh && ./scripts/check_doc_claims.sh
make prove REF=<fix-sha>                              # aborts on a dirty tree; use a detached worktree
```

**Current result, measured on `cfa8a5f`:** origination **730 passed, 1 xfailed**; the three
`redaction-tests` files **206 passed**; import smoke OK; all three doc gates exit 0; ruff
clean. `make prove` PROVEN on all three slices — **but read it narrowly**: slice 2's step 1
was a `ModuleNotFoundError` (collection error) and slice 3's was
`AttributeError: no attribute 'trace'`. Both prove the code is new, not that a subtle
regression reproduces. The genuine red observed this session was the pydantic bug above.

**Not run:** anything against the live stack. No `make up`, no real Bedrock call, no trace
seen in the LangSmith UI. Slice 3's spans are asserted by recording what `assistant.py`
hands to `trace()`, which is the right level for "what do we put on a span" and says
nothing about what LangSmith renders. **Do not claim the root trace is demonstrated until
it has been seen.**

## Branch state (cite the client's real baseline)

- `main` @ `06c27a3` = the client's real state. Carries `search_policy` + ADR 0019 (#64),
  the Bedrock region pin + proof harness (#59), and the D19 idempotency schema rung (#63).
  Still carries the live debt: ADR 0010 ownership IDOR, CVV retention (D13), float money
  math, no ledger.
- `fix/demo-runtime`, `feat/agentic-trace-week10`, `feat/agentic-root-trace` = proposed
  changes on top, in that dependency-free order.
- Cite `git show main:<file>` for client-current state, never a working branch.

## Debt log refs

- No entries opened or closed. D16 (pgvector deferred, in-memory exact cosine) and D28
  (equal nested timeouts) are named in slice 7's known-limitations list, not fixed.
- D19 is half landed: the schema rung merged as #63; the capture-side fix is #65, open.
  D3 (lost update) still has no fix code anywhere.

## Next session: start here

Add a `tools` field to `CompletionRequest` (`app/llm/adapter.py:37`) and thread it through
`build_request` (`app/llm/request_builder.py:507`) — schemas only, no loop change, no
`create_agent` yet — and write the `_redacted_turn` rule for tool-call and tool-result
blocks with its tests. That is slice 4's first half and the only part everything else waits
on.
