# AI Systems Architecture Audit — Meridian Lending Platform

Date: 2026-08-31 · Auditor: Claude (Principal AI Systems Architect / SRE role) · Base: `origin/main` @ `f4fb151`

Scope: the officer-assistant agentic loop (`services/origination-service/app/assistant.py`),
RAG policy retrieval (`policy_retrieval.py`, `rag_eval/`), the LLM transport layer
(`app/llm/`), and the surrounding money-moving services (decision, disclosure, servicing,
payment) and the synchronous HTTP mesh that ties them together. Every finding below is
grounded in code at the cited path — no speculative architecture assumed. The roadmap this
audit produced was split into an immediate and a medium-term handoff, both session-local;
section 3 below carries every item from both, with its status and the commit that closed it,
and supersedes them.

---

## 1. Risk Matrix

| Component/Layer | Vulnerability / Gap | Impact | Trigger Condition | Status |
|---|---|---|---|---|
| `origination-service/app/clients.py` | No circuit breaker across LOS→KYC/decision/disclosure hops; single 30s timeout, no retry/backoff for non-LLM calls | High | A downstream service degrades but doesn't hard-fail — every applicant-facing request blocks the full 30s before erroring | **Closed** — §3 item 7 (`127d1f3`+`61e35dc`, PR #137) |
| Same, D28 | Outer LOS timeout (30s) equals the inner bureau-pull timeout inside decision-service (30s) — the outer budget can never expire first | Med | A stall inside the bureau call can't be attributed to the hop that owns it; origination's timeout is structurally dead | **Closed** — §3 item 5 (`6457328`, PR #139) |
| `policy_retrieval.py::_index()` | Corpus index built once per process, cached forever in a module global with no TTL/invalidation | Med | An operator updates the bind-mounted policy corpus; every long-lived container keeps serving the stale index until restart — silent drift, no signal | **Closed** — §3 item 4 (`b316f83`, PR #136) |
| `policy_retrieval.py::_build_index()` | First request after boot pays the full embed cost for every chunk (lock-serialized, not pre-warmed) | Low | Cold start under concurrent officer load — first N requests queue behind one lock while embeddings build | Open |
| `assistant.py::run()` | Step-exhaustion refusal well-guarded (both langgraph soft/hard stop shapes handled), but a refusal after `score_application` already ran leaves a persisted `decision_events` row the officer never sees | Med | Model exhausts steps on the *validate/narrate* turn after the regulated decision already recorded — record exists, officer gets a refusal, not the outcome | **Closed** — §3 item 3 (`87bd68c`, PR #136) |
| `llm/transport.py::call_with_retry` | Timeouts are explicitly not retried (documented policy) — a transient network blip on a long TLS handshake fails the whole officer request | Low–Med | Intermittent network jitter on the Bedrock/Anthropic call; policy trades resilience for correctness (no client-side double-billing) — reasonable, worth periodic revisit per the module's own docstring | Open — accepted policy |
| `llm/config.py` (model pin) | `CLAUDE_MODEL` env var overrides the pinned default at runtime | Med | A deploy sets `CLAUDE_MODEL` to a typo'd or deprecated model id; first sign is a provider 4xx storm at runtime, not a boot-time config error | **Closed** — §3 item 2 (`f97775d`, PR #136) |
| `rag_eval/run.py::_pick_judge` | `RAG_JUDGE` defaults to `none` — the blocking `rag-eval-gate` runs the keyless TF-IDF path by default; every model-graded axis reports `not_evaluated`, not pass/fail | High | Any prompt or retrieval regression on a model-graded axis (faithfulness, relevance) ships green because the gate never actually asked a judge | Open — §3 item 1 |
| `services/gateway/app/main.py` | Gateway authenticates but does not enforce role authz on money actions (documented in-code as "kept on purpose") | High | Any authenticated session can call money-moving routes the gateway proxies; authz is pushed downstream to each service, inconsistently | Open — §3 item 12 |
| `servicing-service` `adjust_balance`/`waive_fee` (D32) | Unlocked read-modify-write on `balance`/`past_due`, unlike the atomic `apply_payment` path | High | Concurrent adjust/waive calls on the same account lose an update — same shape as the fixed D3 bug, un-fixed here | **Closed** — §3 item 6 (`cd71243`, PR #138; `2452f80`, PR #144) |
| Assistant trace boundary | Root trace (`assistant.entry`, `app/main.py`) opens at the route funnel; a refusal raised before the funnel (gateway hop, pre-funnel 403/422) is untraced | Med | An audit trying to reconstruct "why did the officer get refused" from LangSmith alone hits a gap for the earliest failure class | **Closed, scoped down** — §3 item 8 (`9a52f2f`, PR #137) |
| `services/*/app/redactor.py` | Duplicated per service, no shared package; drift only caught by a CI gate, not structurally prevented | Med | A hand-edit to one copy that skips `scripts/sync_redactor.sh` diverges until the next PR trips `redactor-drift` | Open — §3 item 9 |
| Backend matrix CI job | `continue-on-error` + `\|\| true` on the general pytest step | Med | A money-math regression outside the specifically carved-out blocking gates (tila-vectors, reconciliation, atomic-apply) ships silently green | Open — tolerated by design |
| `policy_retrieval.py::search()` | No cross-request cache on repeated identical queries (only within-run caching via the tool closure's `state["searches"]`) | Low | Two different officer runs asking the same policy question both pay a live embed call — a cost/latency issue, not a reliability one | Open |

---

## 2. Deep-Dive Gap Analysis

> **This section is the analysis as of 2026-08-31; its argument is not rewritten as findings
> close, only its reversed factual claims are.**
> Seven of the fourteen matrix rows above have since been closed. Where section 2 argues a
> gap and the Status column says **Closed**, section 3 governs — it names the commit and PR.
> Every paragraph whose factual claims the fixes reversed outright is corrected in place
> below and marked.

### 2.1 System Reliability & Fault Tolerance

**Determinism & drift.** The LLM layer is disciplined relative to most systems this shape:
`temperature=0.0` is the pinned default (`llm/config.py:165`), the model id is pinned to a
literal (`_DEFAULT_MODEL` / `_DEFAULT_BEDROCK_MODEL`) rather than a floating alias, and
`CLAUDE_MODEL` overrides were the only unvalidated drift vector — closed 2026-08-31 (§4 below),
mirroring the allowlist already applied to `AWS_REGION` (`llm/config.py:194-206`, allowlisted
after a documented incident, RGN-001/002).

Reproducibility for debugging is strong: every span stamps `execution_mode`
(real/fixture/fallback), `estimated_input_tokens`, `trimmed_history_turns`, provider, and
region (`llm/client.py:210-256`), so a trace answers "what actually ran" without needing to
reproduce the call. The gap is the policy corpus: it is not versioned into the trace at all — a
retrieval hit's span carries `chunk_id` and `score` but no corpus version/hash, so two traces
showing the same `chunk_id` on different days aren't provably the same text if the corpus was
updated. (This originally read "compounded by the `_index()` staleness gap below — a
restart-only reload means a hash isn't even a differentiator most of the time." **Corrected
2026-09-02:** the index carries a 900-second wall-clock TTL, §3 item 4, `b316f83`, PR #136. The
corpus is still unversioned in the trace, which is the gap this paragraph is actually about, and
the TTL narrows the window rather than closing it.)

**Cascading failures & circuit breaking.** The weakest pillar. `origination-service/app/clients.py`
gives every downstream call (KYC, decision, disclosure) one flat 30s timeout via a module-level
`_TIMEOUT` constant, and — confirmed by grep across all seven services — there was no circuit
breaker anywhere in the system. The already-tracked D28 debt item (equal nested timeouts:
origination's 30s outer budget equalled decision-service's own 30s bureau-pull timeout) meant the
outer timeout was structurally dead — it could never fire before the inner one did, so a stalled
bureau call always looked like a decision-service failure, never an origination-level budget
exceeded.

**Corrected 2026-09-02.** Both claims in that paragraph are now false, and this is the pillar
the audit moved most. A per-downstream circuit breaker keyed by base URL exists (§3 item 7,
`127d1f3`+`61e35dc`, PR #137), and `decision-service`'s `_BUREAU_TIMEOUT` is 20s against
origination's 30s `_TIMEOUT` (§3 item 5, `6457328`, PR #139), so the outer budget can now
expire first and a bare timeout is attributable to the hop that owns it. What remains open from
this paragraph is the last sentence: there is still no retry or backoff on the non-LLM
downstream calls. There is also no retry/backoff on any of these calls (only the LLM transport layer
has one), so a single transient 503 on KYC fails the whole intake request with no recovery.

The LLM layer, by contrast, has a real retry policy: exponential backoff with equal jitter,
retry scoped to HTTP 429/5xx only, explicit non-retry on 4xx and on timeouts
(`llm/transport.py:1-19`, policy documented and justified). This is a correct, narrow circuit —
but it is a single-call retry, not a breaker with open/half-open state; a sustained provider
outage burns `max_retries` (default 3) on every single officer request rather than tripping to
a fast-fail state after the first few failures. At current traffic that's likely fine; it
becomes a real cost/latency problem at higher concurrency, where every request pays the full
backoff ladder independently.

**Tool & agent orchestration.** The standout strength of the system. The agent loop
(`assistant.py`) enumerates and closes five separate interlocks against known agentic failure
modes:

- **Interlock 1** — one regulated decision per run (`_score()` serves the cached result on a
  repeat `score_application` call, `assistant.py:407-427`)
- **Interlock 2** — explain tasks never trigger a fresh billable credit pull (`task ==
  "explain"` branch reads the persisted record instead)
- **Interlock 4** — step exhaustion, handled on both shapes langgraph can produce: the soft
  stop (`create_react_agent` appends framework prose and returns normally — caught by
  `_terminal_action` refusing to treat that prose as a final answer) and the hard stop
  (`GraphRecursionError`, caught explicitly). The code comment documents that both shapes were
  independently measured on the pinned langgraph version — this couples correctness to
  framework internals that can change silently on a langgraph upgrade, so it needs a
  pinned-version regression test tied to that library version, not just to behavior.
- **Interlock 5 (PT-001)** — `search_policy` capped at one call per run, repeat calls served the
  cached answer
- **Native tool-call enforcement** — `llm/client.py::_tool_action` refuses more than one tool
  call per turn and refuses a tool name the request didn't actually bind, closing the gap where
  a provider ignores `disable_parallel_tool_use`

The one orchestration gap: if the model calls `score_application` (which durably persists a
`decision_events` row) and then exhausts its step budget on the narrate/validate turn, the
regulated decision is recorded but the officer receives a refusal, not the outcome.
`_validated_final` would show it correctly on a subsequent request (idempotency key
permitting), but the immediate UX is "the assistant failed" over a decision that actually
succeeded and was recorded. This is a UX/observability gap, not a correctness one — the record
is safe. (**Corrected 2026-09-02:** this originally read that nothing surfaces "a decision was
recorded on this failed run" to the officer or to a log line at refusal time. The refusal now
carries `scored=True/False`, so a decision recorded on a failed run is visible at refusal
time — §3 item 3, `87bd68c`, PR #136.)

**Silent degradation & recovery.** The strongest control in the whole system: `_validated_final`
(`assistant.py:528-654`) never trusts the model's narration. It re-fetches the persisted
decision record and only serves the model's structured claim if it matches; on mismatch, the
recorded facts win, the narration is discarded, and `narration_validated: false` is logged and
traced. This is the textbook fix for "hallucinated payload with valid JSON syntax." The same
posture appears in `_policy_section` — the model never even receives the corpus text, so it
structurally cannot paraphrase or misquote a policy passage; the officer reads code-rendered
verbatim text.

The corresponding gap was that this pattern is not applied to the two ungated servicing writes:
`adjust_balance` and `waive_fee` (D32) kept the unlocked read-modify-write shape the D3 fix
eliminated from `apply_payment`. This is not an LLM problem, but it is the same "silent
degradation" class — a concurrent write loses an update and nothing detects it until
reconciliation. (**Corrected 2026-09-02:** both writes are now atomic — `waive_fee` by atomic
decrement, `adjust_balance` by compare-and-set — §3 item 6, `cd71243` PR #138 and `2452f80`
PR #144. What remains of this paragraph is the detection side: reconciliation still does not
cover balance adjustments the way it covers payment capture-vs-ledger.)

### 2.2 Observability & Telemetry Infrastructure

**Tracing & span continuity.** Span coverage from officer request to model call is end-to-end:
`assistant.entry` (main.py) → `assistant.request` (loop root) → `llm.complete` →
`llm.transport` → `tool.*` per dispatch → `policy.retrieval` → `assistant.validate`. Each layer
has a documented, enforced content boundary (the "CONTENT RULE": enum codes, integers,
booleans, scores — never applicant data, model prose, or the model-authored policy query), and
`harden_trace_client()` (`llm/config.py:323-372`) is an unusual and effective control: it primes
the LangSmith singleton with `hide_inputs=True, hide_outputs=True` before any framework tracer
can claim it first, specifically because a measured run showed the framework's own tracer
leaking the model's prose and the policy query onto spans the hand-written code never would
have. It also actively refuses `LANGSMITH_HIDE_METADATA=true` as a misconfiguration, because
that setting would blank the exact enum-only signal the whole design exists to preserve.

The documented gap (already known, in CLAUDE.md/ADR 0021): trace coverage starts at
`assistant.entry`, the route funnel — the gateway hop and any refusal thrown before that funnel
(auth, pre-funnel 403/422) are untraced. For a compliance audit trying to reconstruct "why was
this officer request refused," that is a real blind spot, distinct from the LLM-specific
content-boundary gaps above. (**Corrected 2026-09-02:** closed and scoped down — investigation
found most of the apparent gap deliberate and pinned by an existing test, and the one genuine
case, `get_llm_client`'s 503 when `LLM_ENABLED` is off, got a structured log line rather than a
span — §3 item 8, `9a52f2f`, PR #137.)

**Key telemetry metrics.** Present: token usage and cost (via LangSmith's
`ls_provider`/`ls_model_name` tagging, `transport.py:36-57`, with a documented
canonicalization fix so Bedrock inference-profile ids still price correctly), retry counts
(`retries["n"]` logged and available for a retry-rate metric), latency (`latency_ms` on every
`llm ok`/`llm call failed` log line), validation failure and fallback usage
(`run_tree.metadata["validation_failed"]`/`fallback_used`), tool dispatch counts including
cache-hit/served-from-cache marks (`_tool_span`'s `served_from_cache` marker — a de facto
cache-hit-rate signal, just not aggregated into a dashboard metric).

Absent: this originally read that there is no p95/p99 latency aggregation and no guardrail
refusal *rate* as a first-class metric. (**Corrected 2026-09-02:** both ship — the officer-gated
`GET /assistant/metrics` serves p50/p95 latency and `refusal_rate_among_recorded_runs` over
`assistant_runs`, §3 item 10, PR #151, `d4d9efb`.) The residual gap is narrower: no scheduled
pipeline — nothing reads that endpoint on a cadence and no Prometheus/StatsD emission point
exists in this service — and no cache-hit or retry metrics, though both underlying signals are
recorded (`served_from_cache` on tool spans, `retries["n"]` on the transport). No
evaluation-score-delta-over-time signal is wired into the CI or runtime path — `rag_eval`'s graded pass is a point-in-time run, not a continuously
tracked trend, and (per the risk matrix above) it defaults to *not evaluating* anything unless
`RAG_JUDGE` is explicitly set.

**Decision & audit records.** Strong. `assistant_runs` (migration 0021,
`assistant-telemetry-gate` in CI) persists the officer request outcome; `_score_application`'s
regulated write goes through decision-service's append-only `decision_events` atomically with
the score itself — a model literally cannot decision an application without the record existing
(enforced structurally, not by convention, per the module docstring). Prompt version
(`template.name`, `template.version`) is logged on every LLM call. What an independent auditor
cannot reconstruct from logs alone: the exact system-prompt state and exact context payload sent
to the model, by design — that is the content-boundary tradeoff (privacy over full replay). This
is a defensible choice for a regulated-lending context (PII must not leave the building), but it
does mean "prove exactly what the model saw" requires `LLM_TRACE_CONTENT=true` in a
non-production environment, which is correctly gated off by default and structurally refused in
production (`config.py:283-298`).

### 2.3 Guardrails & Evaluation Integrity

**Input/output validation.** Native tool schemas (`_NoArgs`, `_PolicyQuery`) close the
free-text-injection surface almost entirely — the model has exactly one string input in the
whole system (`search_policy`'s `query`), length-bound to the prompt's own declared schema via
a read-not-duplicated lookup (`assistant.py:359-365`) so the bound cannot silently drift between
the prompt contract and the tool schema. `_is_text_tool_action`/`_tool_action` in `llm/client.py`
close the "model writes a tool call as prose instead of a real `tool_use` block" gap explicitly
— exactly the kind of thing that looks like it works in testing and silently degrades in
production as providers change their tool-calling behavior. Output-side,
`validate_structured`/`guard_output` run before any result is trusted, and a validation failure
is distinguishable in telemetry from a healthy call (`validation_failed`/`fallback_used`
markers) rather than reading as a normal green trace.

Sensitive-data leakage: the redactor is applied at multiple boundaries (`build_request` for
prompt variables, the logging formatter as defense-in-depth, explicit query-never-logged
handling in `policy_retrieval.search`). The known structural weakness is the redactor's
duplication across all seven services with no shared package — CI's `redactor-drift` gate is a
detective control, not a preventive one; nothing stops a hand-edit to one copy between CI runs
on a fast-moving branch.

**Continuous evaluation.** The pillar with the most daylight between "designed well" and
"actually gates anything." `rag_eval/evaluator.py` + `rag-eval-gate` exist and are wired into
CI, but `RAG_JUDGE` defaults to `none` — meaning the blocking gate, as currently configured,
runs the keyless TF-IDF path and reports every model-graded axis as `not_evaluated` rather than
pass/fail. The one full graded pass on record used an explicit judge and pinned Titan-based
abstention checks, and it caught something real (Haiku fencing its JSON output, PR #119) —
proving the harness works when actually invoked with a judge. But "works when invoked" and
"gates every merge" are different claims, and right now the CI default is the former, not the
latter. Still open — §3 item 1. There is no live-production monitoring loop feeding back
into this evaluator (no shadow-eval on real officer traffic, no drift alarm
comparing live retrieval score distributions against the offline-measured threshold, which is
itself documented as non-portable across corpus or embedder changes).

---

## 3. Status (verified against `origin/main` git history at `f4fb151`, not memory)

Between this report's first draft and this update, seven of the eight immediate/medium-term
items landed on `main` — not by this session; ground-truthed via `git log`/`git show` against
`origin/main` before writing anything below, same discipline the debt-log status rules require.
Each entry cites the real commit and PR; none of this is credited to the wrong author.

### Addressed

- **Item 2 — `CLAUDE_MODEL` allowlist at boot.** `f97775d`, PR #136
  (`fix/audit-immediate-hardening`, merged `98715eb`). `_KNOWN_ANTHROPIC_MODELS`/
  `_KNOWN_BEDROCK_MODELS` frozensets checked in `load_llm_config()`; 3 new tests in
  `test_llm_client.py` (unknown model rejected, known override accepted, per-provider
  cross-check rejected).
- **Item 3 — surface `scored=True/False` on step-exhaustion refusals.** `87bd68c`, same PR
  #136.
- **Item 4 — TTL on `policy_retrieval._index()`.** `b316f83`, same PR #136.
- **Item 5 — D28, decoupled timeouts.** `6457328`, PR #139 (`fix/downstream-timeout-budget`,
  merged `864147c`). Shipped differently than this report's fix direction proposed:
  decision-service's own bureau-pull timeout was *shortened* to 20s (not origination's outer
  budget widened), and `run_decision` gained the missing `httpx.HTTPError` catch it lacked —
  a bureau stall now surfaces as decision-service's own bounded 503 within budget, and
  origination's own budget genuinely expiring is a distinct, now-caught failure.
- **Item 6 — D32, atomic `adjust_balance`/`waive_fee`.** Landed in two parts: `waive_fee`
  (`cd71243`, PR #138 `fix/servicing-lost-update`, merged `530aa61`) got the same
  atomic-decrement shape as `apply_payment`. `adjust_balance` (`2452f80`, PR #144
  `fix/adjust-balance-compare-and-set`, merged `a847abd`) needed a different shape — it sets
  an absolute figure, not a delta — so it shipped as compare-and-set
  (`UPDATE ... WHERE balance = :expected`, 409 + current balance on conflict, frontend
  refetches and requires deliberate resubmit; same no-silent-auto-resolve posture as payment
  idempotency).
- **Item 7 — circuit breaker on LOS→KYC/decision/disclosure.** `127d1f3` + follow-up
  `61e35dc`, PR #137 (`fix/downstream-circuit-breaker-trace`, merged `52723c1`). Per-downstream
  breaker keyed by base URL, trips after 5 consecutive transport failures/5xx, half-open probe
  before closing; the follow-up commit fixed two review findings (5xx wasn't originally
  classified as a failure when no exception was raised; half-open state let a probe burst
  through instead of serializing to one).
- **Item 8 — root tracing gap.** `9a52f2f`, same PR #137. Scoped down after investigation, not
  built as proposed: most of what looked like an untraced gap turned out to be deliberate
  (`require_officer`/`check_llm_rate_limit` refusals stay untraced on purpose, pinned by an
  existing test) or not actually present (gateway's `/los` path has no real auth-refusal gap
  since origination owns role authz). The one genuine gap — `get_llm_client`'s 503 when
  `LLM_ENABLED` is off, which fires before `assistant.entry` opens — got a structured log line,
  not a trace span, matching how the two deliberately-untraced refusals are already handled.

### Remaining

Every item below was re-verified against `origin/main` at `f4fb151` on 2026-09-02, after four
further merges moved the tip past the one this section was first written against. Item 10
changed in that window; the other four did not.

> **Immediate:**
> 1. Default `rag-eval-gate` to an actual judge (`RAG_JUDGE=bedrock` or equivalent). Still
>    open: the `rag-eval-gate` job in `.github/workflows/ci.yml` sets no `RAG_JUDGE` env var,
>    so the blocking gate grades on the keyless TF-IDF path and reports every model-graded
>    axis as `not_evaluated`.
>
> **Long-term** (not handed off originally, and unscoped except where noted):
> 9. Extract the per-service redactor into a shared package. Still open — seven copies of
>    `services/*/app/redactor.py`, held in step by the `redactor-drift` gate rather than by
>    structure.
> 10. Build a metrics pipeline for guardrail refusal rate, tool cache-hit rate, retry rate.
>     **Refusal rate shipped** as the officer-gated `GET /assistant/metrics` aggregate over
>     `assistant_runs` (PR #151, `d4d9efb`), which serves
>     `refusal_rate_among_recorded_runs` plus p50/p95 latency. Tool cache-hit rate and retry
>     rate are still unserved, and there is still no pipeline: the endpoint answers on demand
>     and nothing reads it on a schedule. The decision behind that surface is unwritten:
>     `scripts/spec_gate_map.txt` carries a recorded no-spec/no-ADR exception for
>     `assistant_runs.py`, leaving the module docstring and migration 0021's header as the
>     whole design record.
> 11. Wire live-traffic sampling into `rag_eval`'s evaluator for a production drift signal.
>     Still open, still unscoped.
> 12. Resolve the gateway's documented "no role authz on money actions" gap. Still open, and
>     still the single highest-impact row in the matrix: `services/gateway/app/main.py:7`
>     reads "the gateway authenticates the caller but does NOT enforce role authz", with
>     "(weak authz — kept on purpose)" three lines below it.
