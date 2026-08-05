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

Standing rules distilled from external review (Codex). Each recurring valid finding becomes a durable rule here so the same class never recurs — review rounds trend toward one pass over time.

- READ this section before generating or editing code; hard constraint, same weight as YAGNI.
- After any review round, `address-pr` promotes each recurring valid finding here (one line, imperative, `[YYYY-MM-DD]` tag). Merge duplicates; skip one-offs and typos.
- A rule stays until proven wrong — then edit or delete it and note why.
- NEVER rewrite this file wholesale — append, or edit in place against the current text. Parallel sessions edit CLAUDE.md concurrently, so a whole-file write from stale context silently drops whatever landed since that context was read; a surgical edit touches only the line it matches and leaves a concurrent session's rules intact.

Rules (append as they emerge):
- A DB-readiness rung must assert the catalog object's DEFINITION, not just its name: a CHECK via `pg_get_constraintdef` (normalized for case/whitespace/parens, `NOT VALID` suffix stripped) and `convalidated`; an index via `indisunique` + table + columns. Every such object has two declaration sites (`db/init/001_schema.sql` and a `db/migrations/` file) and the migration swallows the collision (`duplicate_object`, `IF NOT EXISTS`), so a same-named weaker object makes the migration report success while readiness reports ready over an unguarded column. [2026-08-04]
- A migration's duplicate-object path must compare the existing definition and `RAISE EXCEPTION` on any difference — never `RAISE NOTICE ... skipping` on name alone. [2026-08-04]
- A readiness rung may only demand objects this repository's own DDL creates. Ship the `db/init/001_schema.sql` change and the `db/migrations/` file in the SAME commit as the rung, and assert the parity in a test that reads the real schema file — a rung whose object lives on another branch makes every database built from this repo permanently unhealthy with no migration that could satisfy it. [2026-08-04]
- A verifier must never report success for a path on which it verified nothing. If a check is skipped, that is its own nonzero exit distinct from "checked and failed" (`prove_test.sh`: PROVEN 0, REJECTED 1, ABORT 2, UNPROVEN 3) — a skipped rollback printing PROVEN is the exact claim the tool exists to withhold. [2026-08-04]
- A money-moving write derives the loan and the amount from the stored row it references (`INSERT ... SELECT` over that row), never from caller-supplied parameters; caller-supplied ids stay predicates the `SELECT` must match. Every statement in the transaction asserts its affected-row count via `RETURNING`, and zero rows from either the record insert or the balance `UPDATE` rolls back. Trusting the caller lets an internal or reaper call credit the wrong loan or an amount never captured, and an append-only record with a `UNIQUE` key then makes it permanent *and* blocks the correct retry. [2026-08-04]
- Convert the shared write function, not the route: `grep` the function name and close every caller in the same change. `balance.apply_payment` has an HTTP endpoint and a second in-process caller (`servicing-service/app/payments.py`), so an endpoint-only fix leaves the same defect reachable. [2026-08-04]
- A pre-consummation compliance hold must be enforced on the money-moving path itself, not only refused by the control that issues it: if delivery refuses after boarding, boarding must require delivery. Gate server-side and bind the gate to the specific row being acted on (join the disclosure by `offer_id`, not `app_id`); a disabled button is cosmetic. Gate the *creating* branch only — a replay that finds the existing row must still return it and run its reconcile. Prefer the in-path guard over a table trigger when the predicate is monotone (`delivered` is terminal and frozen by `trg_disclosures_freeze_delivered`), and say so in a comment naming what would break it; a trigger on `loans` would also fire for the internal `/board` hatch and for `db/init/002_seed.sql`'s demo loans. [2026-08-04]

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

## Commands

```bash
cp .env.example .env     # POSTGRES_PASSWORD has no committed default — set it or compose fails
make up                  # docker compose up -d --build (postgres, redis, 7 services, frontend)
make down                # stop
make logs                # tail all containers
make ps                  # container status
make seed                # re-apply db/init/002_seed.sql (init auto-seeds on first `up`)
make config              # validate compose file
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
- **Synchronous service coupling.** Origination blocks on downstream HTTP with no timeout/retry contract — a `decision-service` stall blocks the applicant-facing request.
- **LOS↔LSS seam is a direct cross-schema INSERT** (`origination-service/app/intake.py::board_to_servicing`) — no boarding API/event/contract (ADR 0002).
- **The PII redactor is duplicated per service** (`services/*/app/redactor.py`, no shared package). CI job `redactor-drift` fails if copies diverge — resync with `scripts/sync_redactor.sh`, never hand-edit one copy. Redactor regexes must never use capped separator bounds (`{1,2}`, `{0,3}`) for whitespace/dashes — use unbounded, anchored classes, or padding/split bypasses leak (SSN whitespace runs, split/URL-encoded PANs). Redaction fails closed: on parse/lookup failure, mask rather than pass through.

### CI gate structure (`.github/workflows/ci.yml`)

The `backend` matrix job runs pytest with `continue-on-error` + `|| true` — **money-math test failures there do not block the build** (known-flaky, tolerated). Controls that must not regress have their own **blocking** jobs (no `continue-on-error`): `redactor-drift`, `redaction-tests`, `synthetic-credit-gate`, `db-readiness-gate`, `decision-db-readiness-gate`, `secret-scan` (blocks on leaked secret literals + tracked `.env`), `tila-vectors-gate`, and `frontend` build. When touching a security control (PII redaction, synthetic-credit gate, DB readiness, secrets) or the disclosure compute path, expect a dedicated blocking job — keep its regression test green.

**Exception to the tolerated-money-math rule: the disclosure compute path blocks.** `tila-vectors-gate` runs `disclosure-service` `test_tila_vectors.py` + `test_apr.py` + `test_rules.py` outside the matrix. The disclosed APR carries a Reg Z *tolerance* — exceeding it is a violation, not a rounding nit — and the add-on-vs-actuarial defect (4.5pp, ~36× tolerance, on every loan) survived precisely because the money test that would have caught it ran under `|| true`. Vector expectations are pinned literals from an independent solve; never regenerate them from `compute_apr`.

## Docs & decisions

- ADRs in `adr/` — 0002 (single shared DB), 0003 (store card data), 0004 (service decomposition), 0005 (LLM client), 0006 (logging redaction), 0012 (Decimal/minor units, externalized rule config, FK-as-graph provenance).
- Week 4 in flight on `feature/disclosure-week4`: `docs/spec-disclosure-week4.md` is the source of truth.
- `docs/debt-log.md`, `docs/los-lss-seam.md`, `docs/runbook.md`, `docs/security-remediation-2026-07.md`.
- An "AI underwriting assistant" is planned but not built (`docs/STAGE1-PLAN-AI-ASSISTANT.md`, `docs/spec-ai-assistant-week1.md`); RAG eval harness in `rag_eval/`.

### ADR writing standards

ADRs live in `adr/` (sequential `NNNN-short-title.md` — check the highest existing number and increment). Follow the ADR rules in the global `~/.claude/CLAUDE.md`: Nygard format, business problem before solution, 3+ options with rejection reasons, trade-offs/rollback/risks, the cross-cutting concerns, present-tense/active-voice prose, and plain literal language. The plain-language rule's membership test resolves against the project vocabulary below.

### ADR vocabulary (project-scoped)

The membership test above resolves against this list plus the spec/code/`docs/kb.md`. Update this list when the domain grows — it is the allowlist, not a fixed global one.

Allowed domain terms (use these exactly): LOS (origination), LSS (servicing), BFF gateway, intake, boarding, KYC/CIP, decision/scorecard, disclosure, TILA/Reg-Z, APR, actuarial vs add-on, amortization, origination fee, PII redactor, redactor-drift, fail closed, synthetic-credit gate, DB readiness gate, tila-vectors-gate, Decimal/minor units, ledger, debt D-numbers (e.g. D2), seam, beachhead — established project terms; keep them.

Avoid (do not introduce in ADRs): metaphors and vague labels not grounded in this project — e.g. radioactive, landmine/minefield, toxic, silver bullet, magic/secret sauce, nightmare, footgun, gotcha, bulletproof, rock-solid, hairy, gnarly, janky, game-changer. Name the concrete failure or risk instead.
