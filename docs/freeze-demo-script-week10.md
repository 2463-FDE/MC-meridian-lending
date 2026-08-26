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
cp .env.example .env                # POSTGRES_PASSWORD has no committed default; also gives
                                     # POLICY_RETRIEVAL_MIN_SCORE its working value (0.1609)
export LLM_ENABLED=true             # feature gate for every LLM route — host shell only
export AWS_BEARER_TOKEN_BEDROCK=... # or AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY —
                                     # host shell only, never in a committed file
                                     # CLAUDE_PROVIDER/AWS_REGION need no export: the demo
                                     # override pins bedrock + us-east-1 (see below)
export LANGSMITH_TRACING=true       # required for the trace-id walk in §2 step 4
export LANGSMITH_API_KEY=...        # host shell only, never in a committed file
docker compose -f docker-compose.yml -f docker-compose.demo.yml up -d --build
```

`docker-compose.demo.yml` supplies internal-service tokens, the
`ENVIRONMENT=development`/`ALLOW_SYNTHETIC_CREDIT` gates, and the two non-secret
provider-selection keys: `CLAUDE_PROVIDER=bedrock` and `AWS_REGION=us-east-1`. Those two are
pinned in the file precisely because a forgotten export does not fail — it runs the whole
demo against the direct Anthropic API while the deck cites Bedrock. Both are still
`${VAR:-...}`, so a host export overrides them.

What the override does **not** set, and must come from the host shell: `LLM_ENABLED`,
`LANGSMITH_TRACING`, `LANGSMITH_API_KEY`, and the AWS credential. The exports above are what
turn those on. Confirm before the room fills:

```bash
curl -s localhost:8001/health | python3 -m json.tool     # origination-service healthy
```

That check does **not** exercise the AWS credential. `/health` probes the required secrets and
the database, never the model, and origination builds its Bedrock client lazily — so a stack
started with no credential reports healthy and then fails on the first assistant call in §2.
The credential check is §4's proof run. Do it before §2, not after.

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
   from the §1 export block, not the demo override), and walk the tree live: `assistant.request` → `llm.complete` →
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

**This is the pre-room credential check, not only a receipt.** Nothing in §1–§3 proves an AWS
credential resolves: `compose up` succeeds and `/health` returns 200 without one, and the first
thing that touches AWS is the live assistant call in §2. Run this once, off a clean tree, before
the room and before §2 — not live, since it costs a real bureau/model call and its output should
be captured, not re-run per rehearsal.

```bash
cd services/origination-service
git status --short                     # must be clean — the receipt cites a SHA
export CLAUDE_PROVIDER=bedrock         # script refuses without it, even if §1 already set it
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
  fixture, not a mock) when `LLM_ENABLED=true`, `CLAUDE_PROVIDER=bedrock`, and a live AWS
  credential are present. `CLAUDE_PROVIDER` (and `AWS_REGION`) come from the demo override;
  `LLM_ENABLED` and the credential come from the §1 export block, because a feature gate and
  a secret are not things a committed file should decide.
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
- The trace root is `assistant.entry` (`services/origination-service/app/main.py`), which
  opens at the assistant route funnel — so a refusal raised before the loop starts
  (404/409/502/503) is now a trace with a root, an HTTP status and an enum refusal code.
  What is still outside it: the gateway hop, and the route's own pre-funnel refusals
  (`require_officer` 403, `deny_self_decision` 403, an unknown `policy_topic` 422), which
  are logged and not traced.

## 7. Document truth pass

Done in this same change: `docs/kb.md`'s "Last synced" line and merged-PR list were current
through #68 (2026-08-23); this pass verified and cited #69–#80 (D19 status rescue, the
loop swap, the blocking `agentic-loop-gate`, and the trace surface) with
`git merge-base --is-ancestor` against `origin/main` before writing each SHA, per the
debt-log status discipline (`CLAUDE.md` "Debt-log status vocabulary"). `docs/debt-log.md`'s
D19 entry was checked against the same rule and is already correct (`Mitigated`, citing
`payment-idempotency-gate`) — no change needed there.

## 8. Handoff ownership

**Sole owner: `maha-c`** (the FDE who built this program's weeks 1–10). There is no second
maintainer and no on-call rotation behind any of it — a receiving team inherits one author's
work, and the list below is what they would be inheriting, not a division of labour that
exists today.

| Surface | Where it lives | What owning it means |
|---|---|---|
| Officer assistant loop | `services/origination-service/app/assistant.py`, `app/llm/chat_model.py` | The five interlocks (single score, explain-path substitution, query strip, step exhaustion as refusal, single search) plus PT-001's other half — a `policy_topic` run that reaches `final` having never searched is refused in code, not trusted to the prompt — and their tests in the blocking `agentic-loop-gate` |
| Root trace and its content rule | `app/main.py` (`assistant.entry`, the route-funnel root), `app/assistant.py` CONTENT RULE and the `assistant.request` loop root, `app/llm/client.py`, `app/llm/transport.py` | Every new span key is a decision: enum codes, integers, booleans, retrieval scores and chunk ids only — never an identifier, never prose. And no caught exception may cross the entry span: each is translated to an enum refusal code inside it, because `trace()` would otherwise attach a provider message or an `app_id`-bearing URL to the span |
| Policy retrieval and the corpus | `app/policy_retrieval.py`, `policies/` | The 8-code `policy_topic` vocabulary, the fail-closed score threshold, and the hygiene refusal on a bind-mounted corpus. Corpus CONTENT is Lending Ops' (`policies/fee_schedule.md` names them), the retrieval path is ours |
| Provider selection and the proof | `app/llm/config.py`, `scripts/bedrock_proof.py` | The pinned region, the bedrock-runtime region allowlist, and re-running the proof receipt at whatever SHA is being cited |
| Demo runtime | `docker-compose.demo.yml`, this document | Keeping the pinned provider/region and the export block in step with what the deck claims |

**Status of the thing being handed over.** This is a synthetic training demonstration, not a
production certification. No applicant data in it is real, the credit model is a
deterministic stand-in, and nothing here has been through a compliance review. The blocking
CI gates are real and hold real controls; they are not an assurance opinion.

**What a receiving team should read first, in order:** `docs/kb.md` (orientation),
`docs/plan-freeze-agentic-week10.md` (the decisions and their rejected alternatives),
`docs/debt-log.md` (what is knowingly unbuilt, D-numbered), then this document's §6.
