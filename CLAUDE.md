# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Start here:** read `docs/kb.md` — the knowledge-base entry point (orientation, branch layout, where each artifact lives, open decisions). Open detail files only when its read-when hook matches.

## Project instructions

### Build thin first (YAGNI)

- Build the minimal thing that works. No premature abstraction, no config for a single caller, no speculative interfaces or "future-proofing".
- Don't add a layer/factory/wrapper until the same pattern repeats a 3rd time.
- Prefer editing an existing file over creating a new one. Prefer extending an existing function over adding a parallel one.
- Ask before adding a dependency. Check if the stdlib or an already-installed package does it first.
- No new abstraction, base class, or interface with only one implementation.
- Solve the case in front of you, not the imagined general case. Delete unused options/params.
- When a task looks big, propose the smallest slice that delivers value and confirm scope before writing code.

### Review-finding workflow

When I paste a PR/review comment (bot or human): (1) ground-truth the claim against the actual local code and say VALID or INVALID with file:line evidence — never fix a finding you have not first reproduced; (2) fix only the valid ones; (3) add a regression test and commit the fix + test together, then run `make prove` — it rolls the source back to the parent commit and requires the test to FAIL without the fix and PASS with it, so a test that proves nothing is rejected; (4) run the service's pytest + the relevant blocking CI gate locally; (5) push; (6) give me a paste-ready reply for the PR thread. The `address-pr` skill automates this loop — invoke it rather than re-deriving the steps. Never trust a regression test you did not watch fail first.

### Generate review-clean (single-pass)

Applied while writing code, every turn — goal: code passes external review (Codex) in one round, not several. Do these before handoff, not after.

- Read the house-rules section below (and the Review-finding workflow above) before writing or editing code; treat each rule as a hard constraint, same weight as YAGNI.
- Prove before fix: for any bug or failing case, write the regression test first and watch it fail, then fix — then `make prove` enforces red-without-fix / green-with-fix (see Review-finding workflow). No fix ships without a proven-red test.
- Self-review the diff before handoff: re-read it as a hostile reviewer would — unhandled inputs, missing null/error paths, naming, dead code — and fix the obvious before Codex sees it.
- Mirror the nearest sibling: match the patterns of the existing service under `services/` you're closest to (the per-service redactor copies, the raw-psycopg2-vs-ORM seam, fail-closed config). The duplicated-per-service shape makes divergence easy; reviewers flag it.
- Green gates before handoff: run the service's pytest plus the relevant blocking CI gate locally (redactor-drift, redaction-tests, tila-vectors-gate, synthetic-credit-gate, secret-scan). A blocking-gate failure is a wasted round.
- Small diffs: smallest slice that delivers the job (see YAGNI). Less surface = fewer findings.

### Codex review findings (house rules)

Standing rules distilled from external review (Codex) live in the `house-rules` skill:
`.claude/skills/house-rules/SKILL.md`. They are hard constraints, same weight as YAGNI.

- **Invoke the `house-rules` skill before generating or editing any code** under `services/`,
  `frontend/`, `db/` or `scripts/`. Do not write code from memory of these rules.
- After any review round, `address-pr` promotes each recurring valid finding into that skill
  file (one line, imperative, `[YYYY-MM-DD]` tag), not into this file.
- Edit that file surgically, never wholesale — parallel sessions append to it concurrently.
- A tracked `PreToolUse` hook in `.claude/settings.json` fires once per session on the first
  `Write`/`Edit` under those directories and points at the skill, so a fresh clone inherits the
  reminder. It never blocks. The sibling settings.local.json stays untracked and holds personal
  permission grants — do not move the hook there, or a clone gets the skill with nothing pointing
  at it. (That path is deliberately un-backticked: doc-path-lint requires backticked paths to
  resolve in the tree, and this one never does.)

### Branch and PR naming

One vocabulary across branch name, commit type, and PR title, so each derives from the last.

**Branch:** `<type>/<slug>[-week<N>]`

- `<type>` — the Conventional Commits type of the PR's *primary* change: `feat`, `fix`,
  `docs`, `chore`, `refactor`, `test`, `perf`, `ci`. Use `feat`, never `feature` (10 old
  branches use `feature/`; do not follow them). There is no `security/` type — a secret
  purge is a `fix`.
- `<slug>` — kebab-case, 2–4 words, naming the *thing* not the action: `payments`,
  `dob-validation`, `assistant-ui-panel`. Not `add-payments`, not `fixing-dob`.
- `-week<N>` — only on the weekly program deliverable, so one-off work stays out of that
  namespace. `feat/disclosure-week4` yes; `feat/langsmith-tracing` no.
- Local-only branches that are never pushed may use `wip/`, `demo/`, `backup/`.

**Two structural rules that have cost more than the prefix ever did:**

- **One branch per week.** Spec, ADR, and implementation land on the same branch as separate
  commits. Week 1 split across `feature/llm-foundation-week1` + `feature/llm-client-week1`
  (PRs #1, #3) and Week 2 across `feature/rag-eval-week2` + `feature/rag-eval-impl-week2`
  (PRs #5, #6) — four PRs for two weeks, and neither week has one answer to "which branch?".
- **Always branch from `main`**, never from another feature branch. `main` is the client's
  real state and the only correct base for citing it.

**PR title:** Conventional Commits, matching the lead commit's subject —
`<type>(<scope>): <what changed and why>`. **Type it before clicking Create.** GitHub
prefills the title from the single commit's subject when a PR has one commit, and from the
title-cased branch name when it has several — which is why #1, #2, #3, #5, #6, #7, #8, and
#14 are all called things like `Feature/payments week5`. The default is never right on a
multi-commit PR.

## W7–W10 working agreement

### Stop amending. Merge or split.

PR #12 was 7,009 additions across 46 files when I wrote "stop amending #12, merge it." Four days
later it is **11,575 additions across 72 files** — it grew 65% while the instruction was to stop.

- A PR that is still growing is not a PR, it is a branch with a URL.
- When I catch myself adding to an open PR: either it merges today, or the addition is a new PR.
- My own Tier-2 metric — unmerged hand-written lines at freeze under 1,500 — is failing on this
  one PR alone. That metric was right. Use it.

### WIP

Maximum two open PRs. A third requires closing one.

### Size

Target median merged PR at or under 400 changed lines. Mine is ~1,077. #7 was 11,342 across 97
files. 72 files is past every published reviewability threshold by an order of magnitude —
nobody can review it, including me.

### Keep doing

Every PR description I write is substantive, and description length explains more review-latency
variance than PR size does. That part is right. Do not stop.

## Commands

```bash
cp .env.example .env     # POSTGRES_PASSWORD has no committed default — set it or compose fails
make up                  # docker compose up -d --build (postgres, redis, 7 services, frontend)
make down                # stop
make logs                # tail all containers
make ps                  # container status
make seed                # re-apply db/init/002_seed.sql (init auto-seeds on first `up`)
make config              # validate compose file
make prove               # roll source back to parent commit, run the named regression test: must FAIL without the fix, PASS with it (aborts on a dirty tree — run from a detached worktree, never stash the user's work)
```

Portal: http://localhost:3000 · Gateway docs: http://localhost:8000/docs
Demo logins (password `password`): `admin`, `underwriter`, `csr`, borrower `maria`.

### Tests

```bash
make test                                          # pytest across all 7 services (never fails: each has || true)
cd services/<svc> && python -m pytest -q           # one service
cd services/<svc> && python -m pytest tests/test_redactor.py -q          # one file
cd services/<svc> && python -m pytest tests/test_decision.py -k database_url -q   # by keyword
```

Backend services are Python 3.12 + FastAPI. Run pytest from inside a service dir (imports are `app.*`, relative to that dir). No repo-wide venv — each service has its own `requirements.txt`.

### Frontend

```bash
cd frontend && npm install && npm run dev    # Next.js 15 App Router on :3000
npm run build    # what CI runs
npm run lint     # next lint
```

## Architecture

Full detail in `ARCHITECTURE.md` / `docs/architecture.md` / `README.md`. Big picture:

Brownfield consumer personal-installment-loan platform: a **Loan Origination System (LOS)** + **Loan Servicing System (LSS)** behind one BFF gateway, plus a Next.js portal. Originally 3 services (gateway, origination, servicing); the LOS monolith was partly decomposed (ADR 0004) into 4 more. **Seven** backend services total:

| Service | Port | Role |
|---------|------|------|
| `gateway` | 8000 | Session auth (`/auth/*`) + reverse-proxy to the rest. Does **not** enforce role authz on money actions. |
| `origination-service` (LOS) | 8001 | Intake + LOS→LSS boarding orchestrator; calls kyc/decision/disclosure over sync HTTP (`app/clients.py`). |
| `servicing-service` (LSS) | 8002 | Balances, schedule, delinquency, reconciliation, `POST /accounts/{id}/apply-payment`. |
| `kyc-service` | 8003 | CIP identity check → `kyc_checks`. |
| `decision-service` | 8004 | Sync credit pull + scorecard → `decisions` (outcome only). |
| `disclosure-service` | 8005 | TILA/Reg-Z offer, APR, amortization. |
| `payment-service` | 8006 | Card/ACH charge; then calls servicing `apply-payment`. |

### Non-obvious facts that will bite you

- **One shared Postgres, one schema.** All 7 services talk directly to the same `db/init` tables. Decomposition did not split the data layer. Authoritative DDL: `db/init/001_schema.sql`. `db/migrations/` is hand-tracked and lags the init DDL.
- **Partial ORM migration.** Read paths use SQLAlchemy 2.0 (`models.py` + `database.py`). Money-moving write paths (`intake.py`, decisioning, payments, `balance.py`) still use raw psycopg2 (`db.py`). This seam is intentional and is where the money-handling debt lives.
- **Money is `DOUBLE PRECISION` / float math** throughout, *except the disclosure compute path* (`disclosure-service` `apr.py`/`schedule.py`/`offer.py`), which is `Decimal` end to end and converts to float only at the response schema and the `offers` columns (ADR 0012 beachhead; debt D2 partially mitigated). `balances` is a single mutable column, no ledger.
- **The origination fee has one source:** `policies/fee_schedule.json`, read via `disclosure-service/app/rules.py`, which **fails closed** (no default rate; `/health` reports unhealthy). It replaced three drifted hardcoded copies. Never reintroduce a module-level fee constant, and keep the JSON in step with `policies/fee_schedule.md` — a test asserts they agree.
- **Synchronous service coupling.** Origination blocks on downstream HTTP — a `decision-service` stall blocks the applicant-facing request. There *is* a timeout (`services/origination-service/app/clients.py`, one module-level 30s on every call), but it equals the 30s the bureau pull uses inside decision-service, so the outer budget can never expire first and a stall cannot be attributed to a hop. No retry or backoff anywhere in origination — correct for the billable bureau pull, unexamined for the rest. Debt D28; load behaviour past ~20 concurrent applications is ADR 0009 §6, deferred.
- **LOS↔LSS seam is a direct cross-schema INSERT** (`origination-service/app/intake.py::board_to_servicing`) — no boarding API/event/contract (ADR 0002).
- **`TestClient` needs `httpx`, and a multi-step gate job can hide a missing pin.** `fastapi.testclient.TestClient` raises `RuntimeError: The starlette.testclient module requires the httpx package` without it. **All seven services now pin `httpx==0.28.1`** — `servicing-service` and `kyc-service` were the last two. Steps in one job share a Python environment, so a step whose service under-declares its requirements can pass on a package an *unrelated earlier step* installed: `kyc-enforcement-gate` ran `kyc-service`'s `tests/test_kyc_persistence.py` (five `TestClient` uses) after installing `origination-service`'s requirements, green on a dependency no manifest in its own service declared. That step is now its own job, `kyc-persistence-gate`, so its install line is the only one that runs. When you add a gate, split a job, or reorder its steps, a service missing a pin fails at **collection with exit 2** — a missing package, not a broken assertion. A green local run proves nothing here: verify in a fresh venv with the gate's exact install line.
- **The PII redactor is duplicated per service** (`services/*/app/redactor.py`, no shared package). CI job `redactor-drift` fails if copies diverge — resync with `scripts/sync_redactor.sh`, never hand-edit one copy. Redactor regexes must never use capped separator bounds (`{1,2}`, `{0,3}`) for whitespace/dashes — use unbounded, anchored classes, or padding/split bypasses leak (SSN whitespace runs, split/URL-encoded PANs). Redaction fails closed: on parse/lookup failure, mask rather than pass through.

### CI gate structure (`.github/workflows/ci.yml`)

The `backend` matrix job runs pytest with `continue-on-error` + `|| true` — **money-math test failures there do not block the build** (known-flaky, tolerated) — but that tolerance covers the `Tests` step only. **The `Import smoke` step in the same job blocks**: it runs `python -c "import app.main"` from the service directory with no `continue-on-error`, and a bare interpreter reads no pytest ini file, so a module-level import of a repo-root package (`rag_eval`) fails there while the service's whole pytest suite stays green locally. Controls that must not regress have their own **blocking** jobs (no `continue-on-error`): `redactor-drift`, `redaction-tests`, `synthetic-credit-gate`, `decision-idempotency-gate`, `offer-guard-gate`, `adr-0010-authz-gate`, `gateway-trust-boundary-gate`, `compose-hardening-gate-tests`, `compose-hardening-gate`, `kyc-enforcement-gate`, `kyc-persistence-gate`, `payment-idempotency-gate`, `atomic-apply-gate`, `db-readiness-gate`, `decision-db-readiness-gate`, `migration-numbering-gate`, `disclosure-lifecycle-gate`, `rag-eval-gate`, `rag-eval-import-gate`, `secret-scan` (blocks on leaked secret literals + tracked `.env`), `tila-vectors-gate`, `reconciliation-gate`, `agentic-loop-gate` (the officer assistant's tool-calling loop, retrieval, prompt contracts, trace content rule, and the four loop-swap interlocks — previously the whole agentic surface ran under `|| true`), `doc-path-lint-tests`, `doc-path-lint`, `docs-drift-tests`, `docs-drift` (banned false-claim literals in `README.md`/`CLAUDE.md`/`docs/kb.md`), `spec-diff-gate` (a code area whose spec/ADR has already merged must still have one — existence, not a same-PR diff; pairings live in `scripts/spec_gate_map.txt`, and every tracked `adr/*.md` or `docs/spec-*.md` must be mapped there or carry a line-leading `# EXEMPT:` with a reason, so a new ADR is never a docs-only change), `spec-diff-gate-tests`, and `frontend` build. When touching a security control (PII redaction, synthetic-credit gate, DB readiness, secrets) or the disclosure compute path, expect a dedicated blocking job — keep its regression test green.

`doc-path-lint` asserts every backticked repo-path in `README.md`, `CLAUDE.md` and `docs/kb.md` resolves in the branch's own tree, so a doc cannot point a fresh session at a file that lives only on an unmerged branch. Run it locally with `./scripts/check_doc_paths.sh` — the same invocation CI uses; it grades a doc only if that doc is tracked (a checkout holds tracked files only). All three are now tracked on `main` (`CLAUDE.md`/`docs/kb.md` landed with #12) and the post-#12 paths they cite (`policies/fee_schedule.json`, `disclosure-service/app/rules.py`) exist there, so all three grade and resolve. A reference that is absent on purpose goes in `scripts/doc_path_lint_allow.txt` with a reason and the PR that retires it; an entry there that starts resolving fails the run, so merging a branch means pruning its exemptions.

**Exception to the tolerated-money-math rule: the disclosure compute path blocks.** `tila-vectors-gate` runs `disclosure-service` `test_tila_vectors.py` + `test_apr.py` + `test_rules.py` outside the matrix. The disclosed APR carries a Reg Z *tolerance* — exceeding it is a violation, not a rounding nit — and the add-on-vs-actuarial defect (4.5pp, ~36× tolerance, on every loan) survived precisely because the money test that would have caught it ran under `|| true`. Vector expectations are pinned literals from an independent solve; never regenerate them from `compute_apr`.

**Second exception: settlement reconciliation blocks.** `reconciliation-gate` runs `servicing-service` `test_reconciliation.py` + `test_reconciliation_alert.py` + `test_reconcile_cli.py` + `test_reconciliation_utc.py` + `test_runbook_reconciliation.py` outside the matrix. Reconciliation is the only thing comparing what the processor captured against what a loan was credited. **D19 now prevents the exact-retry case at capture** — `payment-service` and `servicing-service` both claim a client-minted `idempotency_key` against a partial unique index via an insert-first `claim_or_branch()` (schema in PR #63, claim path in PR #65) before the processor is contacted, held by the blocking `payment-idempotency-gate` — but reconciliation still catches everything that isn't an exact retry under one key: a processor-side duplicate, a break that predates D19, or D3's lost-update (below). Detection remains the control for those; a regression in it is silent: the report still runs, still exits 0, still looks clean. Keep the exit codes distinct (0 clean / 1 breaks / 2 could-not-run) and keep the alert failing closed on an unset threshold — a well-formed zero from a run that could not read its inputs is the failure this gate exists to catch. **The apply-payment path came out of that suppression on 2026-08-24**: D3's atomic apply merged (#77, `d649bc6`) and `test_lost_update.py`, `test_atomic_apply.py` and `test_payment_applications_ddl.py` now run in the blocking `atomic-apply-gate`. **Still inside the suppression, and able to regress on a green build: the remaining `balance.py` write paths — `adjust_balance` and `waive_fee` keep the unlocked read-modify-write shape (D32), and only the one characterization test naming that shape is in a blocking gate.**

## Docs & decisions

- ADRs in `adr/` — 0002 (single shared DB), 0003 (store card data, superseded by 0013), 0004 (service decomposition), 0005 (LLM client), 0006 (logging redaction), 0012 (Decimal/minor units, externalized rule config, FK-as-graph provenance), 0013 (payment idempotency + tokenization), 0014 (servicing money controls), 0015 (settlement reconciliation as a control), 0016 (fair-lending monitoring computes outside the platform), 0017 (self-decision anonymous-submission gap), 0018 (interim handling of a double-charged borrower), 0019 (policy retrieval on the assistant loop). 0014, 0016 and 0017 are Proposed and specify work that is **not built**; 0015 and 0019 are Accepted and built. 0012 is Proposed but **built and merged** (PR #12, `6b395cb`) — held at Proposed on purpose, not because it's unmerged: two client answers (APR method of record, tolerance regime) can still change D1/D3, and accepting closes that door. Two others are Proposed and **partly built**: 0013, whose Decision 1 (idempotency enforced by a database constraint) shipped across #63 and #65 while Decision 2 (PAN tokenization, CVV deletion) and Decision 3 (self-serve access) are not built, and 0018, whose prevention-at-capture half is that same D19 work while borrower notification, refund submission and refund status are not built. Nineteen ADR files exist on `main` — check the highest number before writing a new one.
- Weeks 4–10 have merged to `main`. Week 5 and the *original* Week 6 deliverables landed as spec/ADR packages with no `services/` or `db/` change behind them — `docs/spec-payments-week5.md` and `docs/servicing-money-comprehension-week6.md` are the sources of truth for what is specified but unbuilt. Week 6's follow-on authz fix is real code (`services/servicing-service/app/authz.py`), and its lost-update fix (D3) landed 2026-08-24 as PR #77, so what stays unbuilt from that week is maker-checker and the append-only ledger. Week 4's disclosure work, week 7's reconciliation and correlation work, and week 8's governance work are real code on `main`; `docs/spec-disclosure-week4.md`, `docs/spec-observability-week7.md` and `docs/spec-fair-lending-monitoring-week8.md` are their sources of truth. Week 9 is the D19 payment-idempotency fix (#63 schema, #65 capture path) plus D3's atomic apply (#77, `d649bc6`, migration `db/migrations/0019_payment_applications.sql`, held by the blocking `atomic-apply-gate`) — no `docs/spec-*.md` of its own, so ADR 0013 Decision 1, ADR 0020 (open as PR #78) and `docs/spec-payments-week5.md` D1/D2/D3(d) are its source of truth. Note D3(d)'s status predicate as written could never fire against the shipped D19 ordering; the built fix finalizes to `captured` first and downgrades to `captured_unapplied` when the apply refuses. Week 10 is the agentic slice: `search_policy` policy retrieval (#64, ADR 0019), one root trace over the officer assistant (#67, #68), the `policy_topic` closed-vocabulary search channel enforced before an assistant final (#71), native tool schemas and real `tool_use` blocks replacing text-encoded tool calls (#72), the loop swap to native tool calling (#76), the blocking `agentic-loop-gate` (#79), and the trace surface rendered in the frontend (#80), specified in `docs/plan-freeze-agentic-week10.md`.
- `docs/debt-log.md`, `docs/los-lss-seam.md`, `docs/runbook.md`, `docs/security-remediation-2026-07.md`.
- An "AI underwriting assistant" is planned but not built (`docs/STAGE1-PLAN-AI-ASSISTANT.md`, `docs/spec-ai-assistant-week1.md`); RAG eval harness in `rag_eval/`.

### Debt-log status vocabulary

`docs/debt-log.md` is what a fresh session reads to learn which controls exist. A stale status there is worse than a missing entry, because it is trusted.

- **Never describe work as sitting in an unmerged PR.** That claim decays the moment the PR merges and nothing re-reads the entry. D5 said the `PiiRedactor` code was "in a separate PR (feature/pii-redaction) and not yet merged" in three places for five weeks after it merged (PR #2, `1f89ac1`, 2026-07-09) — while the redactor was live in all 7 services behind two blocking CI jobs. Cite the merge commit and the gate that holds the control, not the branch that carried it.
- **Verify before you write either status.** `git merge-base --is-ancestor <branch> <base>` answers whether the code is actually on the base branch; a branch existing locally proves nothing. Same rule as the review-finding workflow above — ground-truth the claim first.
- **Mitigated** means the control ships and something blocking keeps it from regressing. **Fixed** means every row of that entry's own Mitigation Path is done. D5 is Mitigated, not Fixed: the redactor merged, but the rotation/retention, redaction-at-ingest, and backup-audit rows are all still open, and encoded PII stays deferred under D14. Conflating the two retires an entry that still has work in it.
- **Name each residual explicitly.** An entry whose status implies more coverage than the code has is the failure mode this section exists to prevent.

### ADR writing standards

ADRs live in `adr/` (sequential `NNNN-short-title.md` — check the highest existing number and increment). Follow the ADR rules in the global `~/.claude/CLAUDE.md`: Nygard format, business problem before solution, 3+ options with rejection reasons, trade-offs/rollback/risks, the cross-cutting concerns, present-tense/active-voice prose, and plain literal language. The plain-language rule's membership test resolves against the project vocabulary below.

### ADR vocabulary (project-scoped)

The membership test above resolves against this list plus the spec/code/`docs/kb.md`. Update this list when the domain grows — it is the allowlist, not a fixed global one.

Allowed domain terms (use these exactly): LOS (origination), LSS (servicing), BFF gateway, intake, boarding, KYC/CIP, decision/scorecard, disclosure, TILA/Reg-Z, APR, actuarial vs add-on, amortization, origination fee, PII redactor, redactor-drift, fail closed, synthetic-credit gate, DB readiness gate, tila-vectors-gate, Decimal/minor units, ledger, debt D-numbers (e.g. D2), seam, beachhead — established project terms; keep them.

Avoid (do not introduce in ADRs): metaphors and vague labels not grounded in this project — e.g. radioactive, landmine/minefield, toxic, silver bullet, magic/secret sauce, nightmare, footgun, gotcha, bulletproof, rock-solid, hairy, gnarly, janky, game-changer. Name the concrete failure or risk instead.
