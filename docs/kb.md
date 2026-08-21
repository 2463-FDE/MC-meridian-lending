# Meridian Lending — Knowledge Base (entry point)

Compact map over work done so far. Detail lives in the linked files — this page just routes you there. Read this first; open a detail file only when its *read-when* hook matches your task.

**Last synced:** 2026-08-13. **Weeks 1–7 are all merged**, weeks 5–7 as spec packages with no implementation behind them. `main` tip is `23c1ea1`. Merged since the 08-07 sync: #14 (week-5 payments spec, `ad2f34e`), #16–#24 (doc-path lint, skills tracking, docs-drift, regulator watch, loan summary, prove worktree, disclosure delivery, house-rules skill, SSN masking), #25 spec-diff-gate (`d43a6cf`), #26 architecture refresh (`bdb4b0c`), #27 regulator sweep (`f375ef2`), the three week-6 PRs #28/#29/#30 (`8720d94`, `0ca6f1a`, `aee1bf5`) and #31 week-7 observability spec (`23c1ea1`). **One PR open: #32 `fix/servicing-authz`** — ADR 0014 Decision 1, the servicing authorization fix, 11 behind `main`. Plan and merge order: docs/goal-weeks1-4-closure.md (local-only, un-backticked on purpose — see below). *(Any live PR metadata below — CI counts, `mergeable_state` — is a point-in-time snapshot; re-poll GitHub before trusting those numbers.)*

---

## 1. Orientation

- **Client:** Meridian Lending Co. — Dana, VP Lending Ops. Finance brownfield.
- **Repo:** `/Users/maha/Desktop/revature/MC-meridian-lending`.
- **Program:** FDE brownfield track. Weekly loop — read artifacts, find the *latent* problem under Dana's casual ask, ship one small increment, log the debt.
- **Stack:** microservices (gateway, origination, decision, kyc, disclosure, servicing) + RAG eval harness + LLM client wrapper. See `ARCHITECTURE.md`.

### Branch layout (important)
- `main` — untouched brownfield baseline. Holds the *real* debt: plaintext PAN/CVV/SSN logs, hardcoded keys, float money math, false "PCI-compliant/audited" README banner, mutable audit_logs. **Cite `main` (`git show main:<file>`) as the client's current state, never a feature branch.**
- `feature/*`, `feat/*`, `fix/*` — the user's own work on top. Sessions now keep **one `git worktree` per active branch** rather than switching the shared checkout, because several sessions run against this repo at once; the main checkout is often on someone else's branch and dirty (`make prove` needs a detached worktree, never stash).
- **Open, unmerged (2026-08-20):** two doc-accuracy branches, both pushed, neither with a PR opened — docs/debt-log-d5-status (`97b612a`, corrects D5 from "in an unmerged PR" to Mitigated) and chore/debt-log-status-rule (`49850a9`, adds the Debt-log status vocabulary to `CLAUDE.md`). `fix/servicing-authz` (#32) **merged** 2026-08-13. *(Branch names here are un-backticked on purpose — `doc-path-lint` reads a backticked slash-bearing span as a repo path that never resolves.)*
- **Merged and done:** *(the five `docs/…` branch names below are un-backticked on purpose — `doc-path-lint` would read them as repo paths that never resolve)* `feature/llm-foundation-week1` (#1), `feature/pii-redaction` (#2), `feature/llm-client-week1` (#3), `security/purge-committed-secrets` (#4), `feature/rag-eval-week2` (#5), `feature/rag-eval-impl-week2` (#6), `feature/decision-assistant-week3` (#7), `feature/langsmith-tracing` (#8), `fix/redactor-ssn-separator-blindspots` (#9), `feat/apply-form-validation` (#10), `feat/assistant-ui-panel` (#11), `feature/disclosure-week4` (#12), `fix/dob-validation` (#13), `feature/payments-week5` (#14), `chore/prove-target` (#15), `chore/spec-diff-gate` (#25), docs/architecture-refresh (#26), docs/regulator-watch-2026-08-14 (#27), docs/week6-servicing-prompts (#28), docs/servicing-money-controls-adr-week6 (#29), docs/servicing-comprehension-week6 (#30), `feat/payment-reconciliation-week7` (#31).
- Local-only: `backup/security-pre-cleanup`, `demo/week1-4`, `wip/tracker`. **The canonical feature-status-tracker.md no longer lives on any branch** — it moved outside the repo to `~/.claude/projects/-Users-maha-Desktop-revature-MC-meridian-lending/feature-status-tracker.md` on 2026-08-13. `main` still tracks a 1,996-byte 3-row decoy, and the branch that untracks it (chore/untrack-status-tracker) is unpushed, so every checkout still materializes the decoy.

---

## 2. Where things live — read-when map

| Read when you need… | File |
|---|---|
| Full status of every feature (branch, spec, ADRs, test/teeth results, PR round) | `feature-status-tracker.md` |
| What weeks 1–4 owe, the live 0/9 trainer scoreboard, the merge sequence and the standing metrics | docs/goal-weeks1-4-closure.md (local-only) — living doc, `.html` twin + artifact `5c9ed224` |
| What merges when, in what order, and the nine-item feedback scoreboard | docs/goal-weeks1-4-closure.md (local-only) |
| Known security/compliance/arch debt (D1–D13) + mitigation paths | `docs/debt-log.md` |
| How LOS ↔ decision/servicing services talk (the seam) | `docs/los-lss-seam.md` |
| A design decision + its rationale | `adr/` (index below) |
| The spec a feature was built against (source of truth) | `docs/spec-ai-assistant-week1.md`, `docs/spec-rag-week2.md`, `docs/spec-decision-assistant-week3.md`, `docs/spec-disclosure-week4.md`, `docs/spec-payments-week5.md` |
| How a fuzzy client ask was reframed into the real problem | `docs/scoping-payments-week5.md` (Week 5 — the worked example) |
| To prove the payment double-charge is real, not customer confusion | `scripts/repro_double_charge.py` (run it; both defects reproduce) |
| The plan behind a stage/phase (pre-build design) | `docs/STAGE1-PLAN-AI-ASSISTANT.md`, `docs/STAGE1-PLAN-RAG-WEEK2.md`, `docs/PHASE1-BEDROCK-PGVECTOR.md`, `docs/STAGE2-VERIFICATION.md` |
| System architecture (services, data flow) | `ARCHITECTURE.md` (root), `docs/architecture.md` |
| Run/operate the stack locally | `docs/runbook.md` |
| Secret purge + key rotation history | `docs/security-remediation-2026-07.md` |
| Past adversarial review findings | docs/teeth-review-2026-07-10.md (local-only), `docs/llm-client-code-review.md` |
| How review round-trips are run | `docs/review-roundtrip-playbook.md` |
| To resume a stopped session with no re-explaining | docs/handoffs/ (local-only, newest wins) |
| Correct-by-construction invariant catalogs (credential/atomicity/replay/crypto) to build in, not review out | `docs/build-invariants.md` |

### ADR index (`adr/`)
0001 record-architecture-decisions · 0002 single-db-shared-schema · 0003 store-card-data (**superseded by 0013**) · 0004 decompose-origination · 0005 llm-client-design · 0006 logging-redaction · 0007 rag-corpus-hygiene · 0008 retrievable-decision-records · 0009 decisioning-assistant-design · 0010 application-ownership-authorization · 0011 mandatory-kyc-before-decisioning · 0012 decimal-minor-units-and-externalized-rule-config (**Proposed**) · 0013 payment-idempotency-and-tokenization (**Proposed**, superseding 0003; landed on `main` with #14) · 0014 servicing-money-controls (**Proposed**, #29) · 0015 settlement-reconciliation-as-a-control (**Proposed**, #31). All fifteen are files in `adr/` on `main`. Since #25, every tracked `adr/*.md` and `docs/spec-*.md` must be mapped in `scripts/spec_gate_map.txt` or carry a line-leading `# EXEMPT:` with a reason, or the blocking `spec-diff-gate` job fails — a new ADR is never a docs-only change. Numbers are claimed at write time and can collide: 0015 was written as 0014 and renumbered when week 6 took that number the same evening.

---

## 3. Feature state (2026-08-13)

- **Week 1** — LLM client wrapper (timeout/retry/structured output/cost guard) + PII-redacting logging + LOS↔LSS seam map. **Merged** — PRs #1 (2026-07-07), #3 (2026-07-10); secret purge #4 (2026-07-10).
- **Week 2** — RAG retrieval eval harness + corpus-hygiene gate. **Merged** — PR #5 (2026-07-11, spec) + PR #6 (2026-07-15, implementation), after ~24 review-fix commits. Blocking `rag-eval-gate` on `main`.
- **Week 3** — single-agent decisioning assistant + append-only decision record. **Merged** — PR #7, 2026-07-19, after 12 review rounds (idempotency, internal-only service gating, schema-readiness health gates, SSN-fingerprint pepper, offer/boarding state guards). Blocking `decision-idempotency-gate`, `adr-0010-authz-gate`, `kyc-enforcement-gate`, `offer-guard-gate`.
- **Week 4** — auto-generated offer + TILA disclosure. **PR [#12](https://github.com/2463-FDE/MC-meridian-lending/pull/12) MERGED into `main` (merge commit `6b395cb`).** ADR 0012 (Proposed), spec `docs/spec-disclosure-week4.md`. The Decimal actuarial APR fix now lives on `main` — `main:services/disclosure-service/app/apr.py` is the corrected path, no longer the add-on seed. The post-merge delivery-guard work that branch was holding shipped as PR #22 (`11bc7dd`): refuse a chain citing a non-approving decision (`fe5a4c1`) and return 409 rather than 500 on a malformed stored document (`11b6d8a`). Dana asked to automate offer/disclosure generation and said the numbers "look basically right"; `compute_apr` was using an add-on annualization instead of the Reg Z actuarial method and was **4.5pp wrong on every loan** (~36x the 0.125pp tolerance). Correctness became the deliverable. Shipped: Decimal actuarial APR + a finance charge that includes the prepaid fee; one versioned fee schedule behind a fail-closed loader (three drifted constants deleted); TILA vectors in a **blocking** CI gate; the FK-as-graph provenance chain (`disclosures` table, `offers.decision_event_id`, `v_disclosure_provenance`) **and the pipeline read that walks it**; a LangGraph maker-checker pipeline gated by a deterministic recompute; the draft -> in_review -> approved -> delivered lifecycle with reason-code routing and a Reg Z 1026.17(b) timing refusal; the underwriting UI for all of it. Live smoke run against `make up` on a populated pre-0013 volume; it found three container-only defects (see the debt note below). **Not yet run: the two LLM stages against a real key.**

---

- **Week 5** — self-serve payments: **spec package only, nothing built.** `feature/payments-week5` off `main`, ADR 0013 (Proposed, supersedes 0003), spec `docs/spec-payments-week5.md`, scoping `docs/scoping-payments-week5.md`. Dana asked to "just add a payment form" and read the three duplicate-charge tickets as customer confusion. `scripts/repro_double_charge.py` settled that against the live stack: one $100 intent sent 8 ways produced **8 charges totalling $800, all returning `200`, with only $600 credited**. The reframe: idempotency (D19) + PCI (D13, CVV retention is prohibited outright) + one publicly-routed internal endpoint that lets a caller credit a balance with no payment at all (half of D8). The reproduction also found a defect the code read missed — **D3**, an unlocked read-modify-write that loses concurrent applies, cited in code four times since the baseline but never logged and never measured. An idempotency key does not fix it; the balance mutation must become atomic. Self-serve access is designed as a `pay:loan:{id}` capability token reusing the ADR 0010 Phase B pattern, phased staff-assisted → pay-by-link → enrollment-at-sanction, so no identity programme blocks the fixes.
  - **First review round on PR #14 (`929943b`), 2 findings, both valid.** The one worth carrying forward: a spec can promise a behaviour its own schema forbids. D1 said a key becomes reusable after `PAYMENT_IDEMPOTENCY_TTL_HOURS`; D2 specified a permanent partial unique index on `payments(idempotency_key)` with no expiry column and no archival step — so vector R6's second insert conflicts forever and the algorithm replays a day-old response instead. The window was prose, not a mechanism. Closed by making expiry a state transition on the row: an expired key is **retired** (`idempotency_key` set to `NULL`, dropping the row out of the partial index) while the payment row itself is never deleted or archived. **Retirement is terminal-only** — an ACH payment is `submitted` for days and outlives the window, and releasing its key would both free it for a new charge while the original intent is live and destroy the value the stuck-row reaper resolves that row by. That second point was found by the teeth pass *on the fix*, not on the original spec.
  - **PR [#14](https://github.com/2463-FDE/MC-meridian-lending/pull/14) MERGED 2026-08-08 (`ad2f34e`).** 7 files / 3,168 insertions: spec, scoping, ADR 0013, debt log, `scripts/repro_double_charge.py`, its cleanup tests, and a doc-path allowlist entry. **No `services/` or `db/` file is in the merge** — the spec-only framing survives the merge, and week-5 implementation is still unbuilt.

- **Week 6** — servicing money controls (Dana asked for a rep dashboard to adjust balances and waive fees). **Merged as three PRs, 2026-08-12: #28 planning prompts (`8720d94`), #29 ADR 0014 (`0ca6f1a`), #30 comprehension report + tests (`aee1bf5`).** Deliverable was deliberately *not* the dashboard: an AI-augmented legacy-comprehension report (`docs/servicing-money-comprehension-week6.md`), characterization tests pinning today's behavior (`services/servicing-service/tests/test_characterization_balance.py`), a red lost-update test (`tests/test_lost_update.py` — $100 + $200 concurrent applies on $500 leave $400, last writer wins), and ADR 0014 (RBAC + maker-checker + append-only ledger). The client's answers are transcribed in `docs/client-answers-week6-servicing.md`; she deferred the approval workflow and asked for the authorization fix **this cycle**. **Week 6 shipped no behavior change** — on `main` today `adjust-balance`/`waive-fee` still accept any authenticated caller, `balance.py` is still an unlocked read-modify-write, and there is still no ledger. The fix is PR #32, open. The client brief's own lost-update example is wrong (payment writes `balance`, waive-fee writes `past_due` — different columns, no collision); the real one is two concurrent `apply_payment`.

- **Week 7** — payment tracing + settlement reconciliation: **spec + ADR only, nothing built.** PR [#31](https://github.com/2463-FDE/MC-meridian-lending/pull/31) merged 2026-08-13 (`23c1ea1`) — `docs/spec-observability-week7.md`, `adr/0015-settlement-reconciliation-as-a-control.md`, and 9 lines mapping both into `spec-diff-gate`. 934 insertions, no `services/` or `db/` file.

### Two standalone PRs, outside the weekly deliverables

- **PR [#11](https://github.com/2463-FDE/MC-meridian-lending/pull/11) — AI decisioning-assistant panel on the officer detail page. MERGED (`b9bfdc1`).** `feat/assistant-ui-panel`; 768 hand-written lines across 9 files (inside the 800-line cap). Surfaces the Week 3 assistant that merged on #7 — that feature now has its UI on `main`.
- **PR [#13](https://github.com/2463-FDE/MC-meridian-lending/pull/13) — reject an unreadable DOB at the boundary, not after it is stored. MERGED (`477293b`).** `fix/dob-validation`; migration `0011` adds a validated CHECK so unreadable rows cannot persist.

## 4. Open / deferred decisions (session-only — not fully captured elsewhere)

- **ADR 0010 — application-ownership authz (OPEN, needs product sign-off).** Anonymous serial-id IDOR across `GET /applications/{id}` (PII enumeration), `/decision` (credit pull), `/offer`, `/accept` (loan boarding). Real fix = officer-OR-owner check, but needs an applicant identity + signup flow that does not exist (gateway `/auth` is login/logout/me only, users seeded). Officer-only band-aid **rejected** — wrong policy, breaks borrower flow. Defense-in-depth landed instead (decision-state guards on offer/boarding, round 11–12) so a forged/denied app can't be boarded, but the ownership hole stays until identity lands. **LIVE EXPOSURE until then.**
- **Week 5 Q7 — a verified delivery channel (OPEN, decides launch shape, not design).** Pay-by-link needs a way to reach the borrower's contact on file. Migrations `0008` and `0009` both record that this platform has none — which is why neither backfills tokens. Phase 1 (staff-assisted, existing logins) deliberately ships without it, so the idempotency and PCI fixes are not held hostage to procurement. Seven other client questions are recorded in `docs/scoping-payments-week5.md` §6 with assumptions, so a different answer costs a config change.
- **Week 5 — CVV retention is live today.** Not a design question. Every payment since go-live holds sensitive authentication data that may not be retained after authorization. The purge is scheduled independently of, and ahead of, the form.
- **Auto-migration-runner** — pushed to own ADR/PR (ADR 0002 platform decision, all 7 services).
- **Mandatory-KYC-before-decision + fail-closed intake** — ADR 0011; product+compliance availability tradeoff.
- **Deferred lower-priority:** intra-network lateral spoof (gateway-signed token); pg/redis host-published ports.
- **Week 4 client questions still unanswered** (`docs/spec-disclosure-week4.md`, *Client Questions*): APR method of record (assumed actuarial — Reg Z prescribes it), tolerance regime (0.125pp assumed, and deliberately a config value so a different answer costs a config change), and back-book scope (the spec fixes forward; existing offers are never recomputed). None blocks the work.
- **Closure-doc carryover (opened 2026-08-04, none of it blocking).** docs/goal-weeks1-4-closure.md (local-only, un-backticked so the tracked-doc path lint skips it) is a *living* scoreboard: each of the nine trainer items carries `OPEN` / `MERGED` / `RENEGOTIATED-with-reason`, and the `Last updated:` + score line get bumped as PRs land. Three things are still owed on it. (1) The nine are **not yet mirrored** into `feature-status-tracker.md` — that mirror is the local working checklist. (2) The `.html` twin and the published artifact (`https://claude.ai/code/artifact/5c9ed224-627f-4a35-a8f6-3c44bea8c3e6`) are **stale**: the twin was hand-wrapped, not generated, so there is no build step and every `.md` edit drifts it — republish the same path to keep the shared link, never mint a new one. (3) The tier-2 standing six (unmerged-lines-at-freeze < 1,500, PR lead time ≤ 48h, PR size ≤ 800 hand-written, ≥ 3 merges/week, deck claims 100% traceable to `main`, regulator check ≤ 7 days) outlive the closure window and move into the `CLAUDE.md` cadence section on **Mon 2026-08-10** — deferred there by the doc's own exclusion table, not forgotten.
- **Lesson from the Week 4 smoke:** a suite that runs from the repo checkout against a stub session cannot see container-layout or ORM-flush bugs. `make up` found three in one pass — an import-time `parents[3]` IndexError, an unresolvable ORM foreign key, and a NOT NULL violation from a missing server default — all with 400+ green unit tests. Treat the smoke as a gate, not a formality.

---

## 5. Gotchas

- Backend service ports are internal-only; the **gateway is the sole trust boundary**. It strips inbound `X-User-Role` / trust headers to kill spoofing. Internal-only writes require `X-Internal-Service`.
- `INTERNAL_SERVICE_TOKEN` required in kyc/decision/disclosure health; demo override supplies a dev token (`docker-compose.demo.yml`).
- `DECISION_FINGERPRINT_PEPPER` ships **blank** in `.env.example` on purpose — config rejects blank/placeholder so a copied template can't key a reversible SSN HMAC. Dev value lives only in `docker-compose.demo.yml`.
- Schema-readiness health gates: services report `schema_not_ready:<obj>` at `/health` on an unmigrated volume (blocks CI) — covers `decision_events.request_id`, its unique index, both append-only triggers, and `applications.monthly_debt`.

---

## Related sessions
- `local_6c6dca67` — Skill feature workflow integration
- `local_485a6335` — Session handoff
- `local_ca1d6570` — Plugins/skills for simple-first building

## Maintaining this file
One page. When it grows past ~2 screens, push detail into a `docs/` file and leave a pointer here. Update **Last synced** + section 3/4 after each feature lands.
