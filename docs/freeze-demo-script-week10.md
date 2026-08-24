# Freeze demo script — agentic slice (week 10)

Slice 7 of `docs/plan-freeze-agentic-week10.md` §5. Demo script with a rehearsed failure, the
exact-SHA Bedrock proof run, the real/fixture/fallback statement, known limitations, and the
document truth pass (this file, plus the `docs/kb.md` sync in the same commit).

**Model:** Claude Haiku 4.5. Say this aloud during the demo — the plan names it explicitly
(§6) because the presentation should never let a listener assume a larger model without being
told otherwise.

## 1. Before the room

```bash
git checkout main && git pull
git rev-parse HEAD                 # note this SHA — cite it, not "main", during the talk
cp .env.example .env                # POSTGRES_PASSWORD has no committed default
export CLAUDE_API_KEY=...           # or the AWS_BEARER_TOKEN_BEDROCK / key-pair form below —
                                     # host shell only, never in a committed file
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

`docker-compose.demo.yml` is what turns on `LLM_ENABLED`, sets a working
`POLICY_RETRIEVAL_MIN_SCORE`, and wires `LANGSMITH_TRACING` — all three landed in slice 1
(`docs/plan-freeze-agentic-week10.md` §5 row 1). Confirm before the room fills:

```bash
curl -s localhost:8001/health | python3 -m json.tool     # origination-service healthy
```

## 2. Happy path

1. Portal → log in as `underwriter` → Underwriting → open any submitted application.
2. **AI decisioning assistant** panel → select a policy topic from the new dropdown (e.g.
   `debt_to_income`) → **Explain**.
3. Point at the rendered result:
   - **Policy citations** — the chunk id and retrieval score (`underwriting_guidelines#debt-to-income-dti`,
     `0.5303` in the reference run below), proving `search_policy` actually ran rather than the
     model asserting a citation from nowhere.
   - **Trace run**, a UUID. This is the officer-facing replacement for the `application_id`/
     `request_id` navigation stripped from every span in #68's review round — say why out
     loud: neither identifier is allowed on a LangSmith span under the no-identifiers CONTENT
     RULE (`services/origination-service/app/assistant.py:44`), so the trace id is the only thing left that opens the run.
4. Copy the trace id, open it in the LangSmith UI (same project, `LANGSMITH_TRACING=true`
   from the demo override), and walk the tree live: `assistant.request` → `llm.complete` →
   `llm.transport` → `tool.search_policy` → `policy.retrieval` → the next step's `llm.complete`
   → `tool.get_decision_record` → `assistant.validate`. Reference shape (real Bedrock run,
   2026-08-23, `docs/handoffs/2026-08-23-freeze-slices-4b-7.md`):

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

   (The step numbering above predates the loop swap; #76 replaced the hand-emitted
   `assistant.step` spans with the framework's own node runs — the leaf spans and their
   content are unchanged. Re-capture this block from a live run before presenting; do not
   quote the old shape as current.)

## 3. Rehearsed failure — the fail-closed retrieval threshold

Demonstrates that an unset retrieval floor is a refusal, not a silent pass-through.

```bash
docker compose -f docker-compose.yml -f docker-compose.demo.yml \
  exec -e POLICY_RETRIEVAL_MIN_SCORE= origination-service true 2>/dev/null || true
# In-place, no rebuild: unset it on the running container's env and restart just that one
docker compose -f docker-compose.yml -f docker-compose.demo.yml \
  run --rm -e POLICY_RETRIEVAL_MIN_SCORE= origination-service \
  python3 -c "from app import policy_retrieval as p; print(p.POLICY_TOPICS)"
```

Simplest reliable path for the room: stop the stack, `unset POLICY_RETRIEVAL_MIN_SCORE` in the
host shell before `up`, so the demo override's `${POLICY_RETRIEVAL_MIN_SCORE:-}` resolves to
empty and the service starts with the threshold genuinely unset (ADR 0019 fail-closed design,
`adr/0019-policy-retrieval-on-the-assistant-loop.md:73`).

1. Same Explain flow as §2, same policy topic.
2. Expect **no citation** — `policy_searches` shows a `below_threshold`/`threshold_unset`
   reason (the exact code depends on which knob is unset; read it off the response, don't
   pre-narrate the wrong one).
3. The point for the room: the officer sees a **reason-specific line**, not a blank panel and
   not a 500 — the abstain is visible, attributable, and distinguishable from "never searched"
   (PT-001, closed in #71).
4. Restore `POLICY_RETRIEVAL_MIN_SCORE` and restart before returning to the happy path.

## 4. Exact-SHA Bedrock proof run

Run this once, off a clean tree, before the room — not live, since it costs a real bureau/model
call and its output should be captured, not re-run per rehearsal.

```bash
cd services/origination-service
git status --short                     # must be clean — the receipt cites a SHA
export AWS_BEARER_TOKEN_BEDROCK=...    # host shell only; a bare `AWS_BEARER_TOKEN_BEDROCK=...`
                                        # assignment with no `export` is invisible to the child
                                        # process the script launches — this is the mistake the
                                        # script's own docstring warns about
PYTHONPATH=. python3 scripts/bedrock_proof.py --out /tmp/bedrock-proof.json
cat /tmp/bedrock-proof.json | python3 -m json.tool
```

`/tmp/...` because the script's default (`logs/bedrock-proof-<sha>.json`) is gitignored — an
explicit `--out` is what makes the receipt findable after the container/session that produced
it is gone. The receipt names which credential form was used
(`AWS_BEARER_TOKEN_BEDROCK` vs the `AWS_ACCESS_KEY_ID`+`AWS_SECRET_ACCESS_KEY` pair) without
ever printing the credential itself (`services/origination-service/scripts/bedrock_proof.py:87`).

**This run has not been executed in this session** — it needs a live AWS credential this
environment does not hold. Run it from a host shell with real credentials before the
presentation, keep the JSON output, and cite its SHA and timestamp in the deck.

## 5. Real / fixture / fallback statement

State plainly, don't let it be inferred:

- **Real**: the LLM calls in §2–§3 are real Bedrock invocations of **Claude Haiku 4.5** (not a
  fixture, not a mock) when `LLM_ENABLED=true` and a live credential is present — which the
  demo override provides.
- **Fixture**: the test suite (`FakeAdapter`, `native_adapter` in
  `services/origination-service/tests/test_native_script.py`) never calls a real model; it is there to prove the loop,
  interlocks, and content redaction independent of provider availability or cost.
- **Fallback**: `LLM_ENABLED=false` (default outside the demo override) makes every assistant
  route return 503 rather than silently degrading to a fixture in a real request path — there
  is no midpoint where the officer gets a fake answer that looks real.

## 6. Known limitations (state at handover)

Full list: `docs/plan-freeze-agentic-week10.md` §6. Headline points for the room:

- Retrieval is in-memory exact cosine similarity over a 9-chunk, two-document corpus —
  pgvector is D16, deferred.
- The decision path never calls retrieval by design; only Explain does.
- Traces go to LangSmith, not a self-hosted backend (week 3's alternatives recommendation) —
  mitigated by the payload allowlist (`services/origination-service/app/llm/client.py:39-75`,
  `services/origination-service/app/llm/transport.py:61-140`) and the no-identifiers CONTENT RULE.
- `LLM_TRACE_CONTENT` **must be `false`** for the graded run — it is `true` in local
  `.env.local` for debugging and puts prompts/responses on spans, which the requirement
  forbids. `compose-hardening-gate` blocks a *committed* file from enabling it; nothing blocks
  a shell — check this by hand before presenting.
- The trace root opens inside `assistant.run()`, not at the officer's HTTP request — a refusal
  before the loop starts (404/422/503) produces no trace.

## 7. Document truth pass

Done in this same change: `docs/kb.md`'s "Last synced" line and merged-PR list were current
through #68 (2026-08-23); this pass verified and cited #69–#80 (D19 status rescue, the
loop swap, the blocking `agentic-loop-gate`, and the trace surface) with
`git merge-base --is-ancestor` against `origin/main` before writing each SHA, per the
debt-log status discipline (`CLAUDE.md` "Debt-log status vocabulary"). `docs/debt-log.md`'s
D19 entry was checked against the same rule and is already correct (`Mitigated`, citing
`payment-idempotency-gate`) — no change needed there.
