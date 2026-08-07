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
- A state flag that asserts an artifact was produced, reviewed, or sent must be gated on that artifact being persisted with the row, and the artifact must be readable by the role that acts on the flag. `disclosures.delivered_at` recorded a delivery whose document existed only in the generating call's HTTP response, so the reviewer approving it could not open it and a later session had nothing to read — persist the artifact, refuse the transition when it is absent, and render it above the control that acts on it. A returning route that hands back an artifact is not the same as storing it: under maker-checker the approver is a different session. **Every CONSUMER of that flag must re-check the artifact, not trust the flag.** The migration that adds the artifact column leaves already-flagged rows at NULL and a freeze trigger then blocks their repair, so a legacy row carries the flag over content that does not exist — `accept_offer` read `status='delivered'` plus `delivered_at` and boarded a funded loan whose TILA document nobody could produce. Select the artifact (or a presence flag) in the money path's own query and fail closed on the boarding branch. The BORROWER is also a consumer of the same flag — they act on `delivered` by accepting — so "readable by the role that acts on the flag" includes them: give the owner/token-holder a delivered-ONLY read path for the persisted body (drafts stay officer-only, same reason the generator is), and have the applicant UI FETCH and DISPLAY that body before it enables accept, gating on the fetched artifact, not the bare status. `read_disclosure_document` was officer-only while the apply screen enabled accept off `disclosure_status==='delivered'` alone, so the boarding guard was a status flag the borrower could never see the evidence behind. And DISPLAY means the whole regulated artifact, not a readable subset: the apply screen rendered only the delivered document's prose (`heading`/`payment_terms`/`prepayment`) while the borrower accepted TILA figures they never saw — the numeric offer panel above shows the recomputed offer state (`usd()`/`pct()`-formatted), not the immutable persisted `figures.*` strings. Render the persisted figure strings VERBATIM (apr + finance charge + amount financed + total of payments + monthly payment), distinct from the reformatted offer numbers, above the accept control, and assert each is on screen before accept enables. The officer screen (`underwriting/[appId]/page.tsx`) already rendered `${figures.apr}%`/`$${figures[field]}`; mirror the twin. [2026-08-04, extended 2026-08-05, extended 2026-08-05, extended 2026-08-05]
- When a stored figure has a rendered copy, validate the copy at the authoritative boundary, by VALUE and by SPELLING. Parse to `Decimal` and compare numerically so `9.5840` and `9.584` agree, but first require a plain-decimal literal (`^\d+(\.\d+)?$`): `Decimal` accepts `17_460.00`, `+3628.71` and `3.62871E+3`, all of which compare EQUAL to the record and would be printed verbatim to a borrower. An upstream verify stage does not substitute — that check runs in the caller. [2026-08-04]
- Adding a column to a table that ships in both `db/init/001_schema.sql` and a `db/migrations/` file needs THREE edits, not two: the init DDL, the original migration's `CREATE TABLE` (byte-identical — `test_init_and_migration_ddl_are_identical` compares them), and a new migration with `ADD COLUMN IF NOT EXISTS` for volumes where the original already ran. The new migration must then assert the column's `data_type` and `RAISE EXCEPTION` on a mismatch, and the service's readiness rung must probe that type — `IF NOT EXISTS` swallows a same-named column of any type, so a `TEXT` stand-in for `JSONB` reports ready and hands every reader a string. [2026-08-04]
- A pre-consummation compliance hold must be enforced on the money-moving path itself, not only refused by the control that issues it: if delivery refuses after boarding, boarding must require delivery. Gate server-side and bind the gate to the specific row being acted on (join the disclosure by `offer_id`, not `app_id`); a disabled button is cosmetic. Gate the *creating* branch only — a replay that finds the existing row must still return it and run its reconcile. Prefer the in-path guard over a table trigger when the predicate is monotone (`delivered` is terminal and frozen by `trg_disclosures_freeze_delivered`), and say so in a comment naming what would break it; a trigger on `loans` would also fire for the internal `/board` hatch and for `db/init/002_seed.sql`'s demo loans. [2026-08-04]
- An idempotent-replay short-circuit is also the only path a half-written row can ever reach again, so it must repair every field a later change made required — not just the one the last review found. `create_disclosure` returned the existing row before looking at the request, so migration 0013's own documented recovery ("undeliverable until regenerated") was unreachable: the pipeline POSTed a valid document, got a 201, and `document_body` stayed NULL while delivery and boarding stayed blocked. Repair on replay, validate the backfilled value against the figures ALREADY ON THE ROW (never a recomputation — the replay path exists because rules drift between attempts), never overwrite a value that is present, and skip a row a freeze trigger protects (`OLD.status = 'delivered'`) or a legitimate replay becomes a 500. When a migration adds a nullable column that a guard then makes mandatory, name the API path that fills it and test that path. [2026-08-05]
- Record a provenance edge at the moment the authorizing row is known, not at a later stage that has to pick "the latest". `offers.decision_event_id` was closed at disclosure time from the newest `decision_events` row; that table is append-only and `uq_offers_app` means the offer is never regenerated, so a re-decision in between re-parented the offer to a decision that did not produce its terms — and `v_disclosure_provenance` reported `chain_complete: true`. A wrong edge is worse than a missing one: the view flags the missing one. Write the edge on the creating INSERT, validate it belongs to the same application there, echo it in the response so the next stage cites the stored value instead of re-deriving it, and have the consumer require an exact match whenever the row carries one. Keep the same-application fallback only for rows that predate the edge, and check `db/init/002_seed.sql` before making the edge mandatory — its `decisions` rows have no `decision_events`. When the same edge is stored in TWO places (`offers.decision_event_id` walked by the view AND `disclosures.decision_event_id` on the regulated record), a completeness check must COMPARE the two copies, not just assert each is non-null: the write path keeps them equal (create refuses a mismatch, replay closes the offer edge from the record's column), but a legacy/back-book row or an operator backfill can carry two DIFFERENT decisions while every edge is non-null, and `chain_complete = not any-null` then reports a whole chain over a split-brain audit trail. Expose BOTH ids in the view (`d.decision_event_id AS disclosure_decision_event_id`) so the reader compares rather than infers, treat a disagreement as an incomplete chain (a named `missing_edges` reason, not a bare bool), route the report and the delivery guard through ONE helper, and fail closed at BOTH money steps — delivery (disclosure-service) AND boarding (origination `accept_offer` re-checks in its own query, since a row delivered before the delivery guard is frozen by `trg_disclosures_freeze_delivered` and unrepairable). Tighten the view's readiness rung to probe the NEW column via `information_schema.columns` (not just the view's existence) so a volume missing the reshaping migration reports schema_not_ready instead of 500-ing the route. [2026-08-05, extended 2026-08-06]
- When a path starts reading a column a LATER migration added, add that service's readiness rung in the same change. Migrations are hand-applied and lag the init DDL, so on a volume at the earlier migration the new SELECT 500s the whole route instead of naming the gap — the local stack ran 0011 without 0012, so adding `ds.document_body` to `accept_offer` broke boarding there with a bare 500. Probe `data_type`, not just the column name (`ADD COLUMN IF NOT EXISTS` swallows a same-named column of any type), and put the rung in the service whose path reads the column, not only the service that owns it. [2026-08-05]
- A JSONB presence check needs `IS NOT NULL AND col <> 'null'::jsonb`. In Postgres a JSON null is a VALUE, so `'null'::jsonb IS NOT NULL` is TRUE and a bare null-check reports a recorded artifact over one that means the opposite. Verify the truth table against a real Postgres — the service tests stub `db.query`, so no test parses the SQL string. [2026-08-05]
- When the backend gains a repair path, update the screen that reports the broken state in the same change. The officer UI kept a "there is no officer action, it needs an operator" message for a full commit after the replay path started repairing those exact rows, while the button that would have invoked it stayed disabled — so the API could fix the row and the only human who would ask for it was told not to. A comment or alert asserting "no remedy exists" is a claim about behavior and goes stale like any other. [2026-08-05]
- A downstream money record must store the rate/figure the record actually operates on, not whichever disclosed figure sits nearest in the source row. Boarding wrote `offers.apr` (the disclosed actuarial APR, which carries the prepaid fee) into the single `loans.apr` column, and servicing amortized its schedule at it — so the funded loan's schedule contradicted its own TILA disclosure on every fee-bearing loan, because the disclosure amortizes at the NOTE rate (`disclosure-service` proves this: APR-rate schedule sums to 17442.72 vs disclosed 16919.15). Derive the operative value from the authoritative stored snapshot the disclosed figures came from (`disclosures.compute_snapshot.note_rate_pct`), validate it (plain-decimal literal; `note_rate <= apr` per spec D1) and fail closed when absent rather than fall back to the wrong figure, add the column where the two values genuinely differ (single overloaded column can't serve both display and math), and update EVERY consumer that computes from it (servicing's schedule route, not just the write). [2026-08-05]
- A content contract the producer enforces (a prompt/output schema pattern, length cap, digit-free rule) must be re-enforced on the model at the PERSISTENCE boundary, not trusted to the caller — the write path that only checks the figures still stores prose verbatim. `DisclosureDocument` accepted arbitrary `payment_terms`/`prepayment` while `create_disclosure` verified only `figures`, so any internal caller, coordinator change, or replay/backfill could persist prose restating a stale or conflicting number (and a Luhn-valid digit-run past the redactor) while the figure gate passed, then deliver it. Mirror the assembler's constraints (`^\D*$`, same length caps) as Pydantic `Field` facets so BOTH the fresh POST and the replay path — which read `document` off the same model — are refused identically at parse; keep the mirror at least as strong as the producer (verify the regex engine agrees, rust-regex vs Python `re`, on unicode digits) and enforce it at BOTH ends (the producer's output schema AND the persistence boundary), so the producer never emits what the boundary rejects. Close EVERY field the contract covers in one pass, not just the ones a reviewer flagged: `heading` was a digit-free field the prompt already mandated but neither schema enforced, and it came back as a second review round after `payment_terms`/`prepayment` shipped alone. [2026-08-05]
- A backfill/repair path that attaches a required artifact to an existing row must invalidate any review-state the row reached WITHOUT that artifact, not preserve it. Delivery gated on "a document now exists"; the lifecycle deliberately lets a documentless draft advance `draft -> in_review -> approved` (blocking earlier would strand legacy rows), so `_repair_missing_document` writing `document_body` while leaving `status='approved'` let the next `approved -> delivered` send a document the reviewer never saw — the approval covered an empty row. Reset the row to the pre-review state (`draft`) inside the same transaction as the artifact write so it must re-enter review; a downstream gate that checks only presence cannot tell a reviewed artifact from a backfilled one. This closes legacy already-advanced rows too, which a "require the artifact to ENTER review" gate cannot reach. [2026-08-05]
- A route-scoped async handler on a reused screen must capture the route generation (`routeGenRef.current`) into a local BEFORE its first `await` and re-check it after every await before it writes state — and any refresh helper it calls that ALSO guards must receive that captured generation, not re-capture at its own call time. `generateDisclosure`/`transitionDisclosure` called `loadDisclosure()`, which captured the generation when it ran — already the NEW route's generation for a stale in-flight action — so its guard passed and it stamped the previous applicant's regulated APR/document/provenance onto the screen now showing someone else (misattributing a TILA disclosure across applicants). The sibling handlers (`runAssistant`, `acceptAndBoard`) already captured the generation up front; mirror them on EVERY such handler, thread the captured generation into the helper, and gate the trailing `setActionMsg`/`setActionBusy` on it too. [2026-08-05]

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
- **Synchronous service coupling.** Origination blocks on downstream HTTP with no timeout/retry contract — a `decision-service` stall blocks the applicant-facing request.
- **LOS↔LSS seam is a direct cross-schema INSERT** (`origination-service/app/intake.py::board_to_servicing`) — no boarding API/event/contract (ADR 0002).
- **The PII redactor is duplicated per service** (`services/*/app/redactor.py`, no shared package). CI job `redactor-drift` fails if copies diverge — resync with `scripts/sync_redactor.sh`, never hand-edit one copy. Redactor regexes must never use capped separator bounds (`{1,2}`, `{0,3}`) for whitespace/dashes — use unbounded, anchored classes, or padding/split bypasses leak (SSN whitespace runs, split/URL-encoded PANs). Redaction fails closed: on parse/lookup failure, mask rather than pass through.

### CI gate structure (`.github/workflows/ci.yml`)

The `backend` matrix job runs pytest with `continue-on-error` + `|| true` — **money-math test failures there do not block the build** (known-flaky, tolerated). Controls that must not regress have their own **blocking** jobs (no `continue-on-error`): `redactor-drift`, `redaction-tests`, `synthetic-credit-gate`, `decision-idempotency-gate`, `offer-guard-gate`, `adr-0010-authz-gate`, `gateway-trust-boundary-gate`, `compose-hardening-gate`, `kyc-enforcement-gate`, `db-readiness-gate`, `decision-db-readiness-gate`, `migration-numbering-gate`, `disclosure-lifecycle-gate`, `rag-eval-gate`, `secret-scan` (blocks on leaked secret literals + tracked `.env`), `tila-vectors-gate`, `doc-path-lint-tests`, `docs-drift` (banned false-claim literals in `README.md`/`CLAUDE.md`/`docs/kb.md`, with `docs-drift-tests` guarding the gate's own logic), and `frontend` build. When touching a security control (PII redaction, synthetic-credit gate, DB readiness, secrets) or the disclosure compute path, expect a dedicated blocking job — keep its regression test green.

`doc-path-lint` asserts every backticked repo-path in `README.md`, `CLAUDE.md` and `docs/kb.md` resolves in the branch's own tree, so a doc cannot point a fresh session at a file that lives only on an unmerged branch. Run it locally with `./scripts/check_doc_paths.sh` — the same invocation CI uses; it grades a doc only if that doc is tracked (a checkout holds tracked files only). All three are now tracked on `main` (`CLAUDE.md`/`docs/kb.md` landed with #12) and the post-#12 paths they cite (`policies/fee_schedule.json`, `disclosure-service/app/rules.py`) exist there, so all three grade and resolve. A reference that is absent on purpose goes in `scripts/doc_path_lint_allow.txt` with a reason and the PR that retires it; an entry there that starts resolving fails the run, so merging a branch means pruning its exemptions.

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
