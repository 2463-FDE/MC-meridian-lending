---
name: feature-workflow
description: End-to-end feature delivery workflow for the meridian-lending project. Use when starting a new feature that should go through the full disciplined pipeline: plan it (with gap analysis and a decision ledger), verify the plan against a spec file (the source of truth), lock the key decisions as an ADR, scope it to the smallest change via ship-thin before planning, implement in small step-by-step commits, run the teeth adversarial check, run unit and integration tests, run smoke (and browser) tests against the live local stack, update the feature status tracker, and prepare a PR. The workflow STOPS at "PR ready" (it does not push or open PRs — the user does that). When the user later returns with review comments, it resumes by invoking the address-pr skill. Chains the existing ship-thin, teeth, and address-pr skills. Trigger on phrases like "run the feature workflow", "build this feature end to end", "start the full pipeline for", "feature workflow for".
---

# Feature Workflow: Spec -> Plan -> ADR -> Implement -> Teeth -> Test -> PR-Ready

You drive a feature from spec to a PR-ready branch on the meridian-lending project, with discipline at every gate. You do NOT push, open PRs, or perform any remote action — you stop at "PR ready" and hand back to the user. The spec file is the source of truth: when code and spec disagree, the spec wins (unless the user explicitly amends it). Each stage has a gate; do not proceed past a gate that fails — surface the problem and stop.

This skill orchestrates three existing skills:
- ship-thin — scope the change to its smallest form (run in Stage 1, before the plan hardens).
- teeth — adversarial break-it review (run in Stage 5).
- address-pr — resolve pasted review comments against the local branch (run only when the user returns with comments, Stage 10).

## Project anchors (verify before you cite them)

These are the concrete repo files and commands the stages below assume. Confirm each exists in the branch's own tree before relying on it (`git ls-files`, `ls`, `grep <target> Makefile`). If one is absent, say so and follow its fallback — never fabricate a path or a passing gate.

- Base branch: `main`. Feature work happens on a feature branch off `main` (Stage 0).
- ADRs: `adr/` at the repo root — sequential `NNNN-short-title.md` (highest existing is 0010+; increment it). NOT `docs/adr/`. If `adr/` is missing, ask the user where ADRs live before writing one.
- Feature tracker: `feature-status-tracker.md` at the repo root (Stage 9). Create with a header row if absent.
- Live stack: `make up` / `make down` / `make logs` / `make ps` (Stage 8). Portal on :3000, gateway on :8000. `make seed` re-applies seed data.
- Tests: `make test` runs pytest across all 7 services (each `|| true`, never blocks). Per-service: `cd services/<svc> && python -m pytest -q`. Frontend: `cd frontend && npm run build && npm run lint`.
- Prove gate: `make prove` (Stage 6/10 regression proof — rolls source to the parent commit, requires red-without-fix / green-with-fix). If the Makefile has no `prove` target on this branch, say so explicitly and skip the gate — it may be an uncommitted local addition; do NOT claim it passed.
- Build invariants: `docs/build-invariants.md` (Stage 1 step 3a). May be ABSENT on a given branch (untracked). If missing, skip the build-invariants matrix expansion and note it — do not invent invariant kinds.
- Chained skills: ship-thin, teeth, address-pr — invoked via the Skill tool, not shell commands.

Grounding rule: any stage that names one of these must check its presence in the current tree first. A missing anchor is a "flag it and adapt" event, not a silent skip and not a fabricated path.

## Which stages always run vs. only for regulated/money/PII/security changes

Keep the core loop light. Run every time: Stage 0 (spec), Stage 1 steps 0–5 EXCEPT the matrix expansion, Stages 2–4, Stage 5 teeth, Stages 6–9. That is normal feature delivery.

Run ONLY when the change touches a regulated, money-moving, PII, authz, or idempotency surface: Stage 1 **step 3a** (the guarantee-matrix cell tables and the `docs/build-invariants.md` kinds), and the deeper security passes teeth surfaces in Stage 5. Step 3a already gates itself — "If the feature triggers no guarantee matrix (pure-frontend tweak, copy/docs change, refactor with no regulated surface), state that and move on." A pure-UI or docs feature does the core loop and skips the security-matrix machinery entirely. Do not manufacture a matrix for a change that has no such surface.

## Stage 0 — Locate the spec (source of truth)

1. Ask the user for the spec file path if not given. Read it fully. This file is authoritative for scope and acceptance.
2. Extract concrete, checkable requirements from the spec into a numbered acceptance list. If the spec is ambiguous or silent on something you'll need to decide, note it as an open question — do not silently invent requirements.
3. Confirm the working branch. The base for this project is main; the feature work happens on the current feature branch off main. If unsure which branch, ask before proceeding.

## Stage 1 — Plan

0. Ship thin first: invoke the ship-thin skill to fix the single job this feature must do and produce its Cut list (what is deliberately NOT built — speculative config, abstraction layers, extra params, new files/deps). This runs before gap analysis so it constrains the plan. The Cut list is scope you carry forward: any plan item that resurrects a cut item must be justified against the spec or dropped. Reuse-before-create and no-abstraction-until-3rd-repeat apply to every plan item.
   The Cut list NEVER cuts a cell of an in-scope guarantee matrix. A guarantee matrix (authz, idempotency, new-field lifecycle) is atomic: ship-thin drops whole features, never half a guarantee. Cutting "the other entrypoints", "the authorization half", "the concurrency case", or "the NULL/legacy path" as speculative just deletes cells the reviewer will demand back — and by then they are outside the plan and cannot be closed in parallel. If a guarantee is in scope at all, every cell of it is in scope.
1. Gap analysis: compare the spec's acceptance requirements against the current state of the code. For each requirement, identify the gap — what exists, what's missing, what must change. List gaps explicitly; a requirement with no gap is already satisfied and should be noted as such.
2. Decision ledger: for each gap that has more than one reasonable way to close it, record an entry capturing the options considered, the tradeoffs of each, the option chosen, and a clear explanation of WHY it was chosen over the alternatives (e.g. fit with existing architecture, simplicity, performance, risk, consistency with the spec). One entry per non-trivial decision. This ledger is the reasoning record; the ADR in Stage 3 then locks the decisions it produced. Do not skip the "why" — an entry without a justification is incomplete.
3. Produce an implementation plan: the changes by file/module across the full stack (frontend, API, data), the order of work, and the test strategy. Each plan item should trace to a gap from step 1 and, where applicable, a decision from the ledger in step 2.
3a. Enumerate every guarantee matrix the feature triggers, HERE, as explicit plan work items — do not leave it to the teeth pass in Stage 5. For each guarantee the feature must uphold (authz, idempotency, new-field lifecycle, redaction/filter), write out the full cell table from the teeth skill's Phase 2.5 chain-completion rules: every entrypoint × identity/key scoping × payload binding × concurrency (authz: every regulated entrypoint × server-side identity × gateway-not-anonymous × ownership scoping). Every cell is a plan item. Then check `docs/build-invariants.md` if it is present in the tree (see Project anchors — it may be absent; skip this step and note it when it is): if the feature matches any **kind** there (ephemeral-credential, dual-store-atomicity, replay-determinism, crypto-material), paste that kind's matrix in as plan items too — those are the subsystems PR 7 hardened one property per review round, and they are not covered by the four guarantees above. The Stage 3 ADR then records a `Discharges build-invariants: <kinds>` line. This makes Stage 4 a checklist-drain instead of a discovery process, and makes Stage 5 teeth a verification of a full matrix instead of a serial discovery of a sparse one — that ordering is what collapses review rounds (PR 7 leaked 9 authz + 5 idempotency rounds because the cells were found after implementation, not before). If the feature triggers no guarantee matrix (e.g. a pure-frontend tweak, a copy or docs change, a refactor with no regulated/money/PII/idempotency surface), state that and move on — do not manufacture a matrix. This step is gated on a real guarantee being in play, not mandatory ceremony.
4. Map every plan item back to a spec requirement number. Anything in the plan not traceable to the spec is scope creep — flag it and drop it unless the user confirms.
5. GATE: present the ship-thin Job + Cut list, the gap analysis, the decision ledger (with justifications), the plan, and the requirement traceability. Do not implement until the plan is coherent, covers every acceptance item, and adds nothing on the Cut list without justification.

## Stage 2 — Verify plan against spec

1. Walk the acceptance list and confirm the plan satisfies each item. List any requirement the plan does not yet cover.
2. List any open questions from Stage 0 that block implementation. If a blocking ambiguity remains, STOP and ask — do not guess on spec-level decisions.
3. GATE: every acceptance item is either covered by the plan or explicitly deferred with the user's agreement.

## Stage 3 — Lock decisions as an ADR

1. For each significant or contested decision in the plan (architecture, data model, interface contract, tradeoff), write an ADR. Draw on the decision ledger from Stage 1 so the ADR's context and consequences reflect the options and reasoning already recorded.
2. Write ADR files to adr/ at the repo root (see Project anchors — NOT docs/adr/). Use sequential numbering (e.g. adr/NNNN-short-title.md); check the existing highest number first and increment. Standard ADR format: Title, Status (Accepted), Context, Decision, Consequences.
3. The ADR locks the decision: subsequent stages must not silently deviate from it. If implementation reveals an ADR was wrong, stop, amend the ADR explicitly, and note why — do not drift.
4. GATE: ADR(s) written and committed before implementation starts.

## Stage 4 — Implement, step by step, with commits

1. Implement in small, logically isolated steps following the plan order. One concern per step. Small commits are for clean history — do NOT conflate commit granularity with completeness: the steps must drain the ENTIRE matrix cell checklist from Stage 1 step 3a, not just the cells you thought of first. Never push and re-trigger the reviewer with a partially-drained matrix; commit granularly, but a guarantee is not done until every one of its cells is committed.
2. After each step: make a focused commit with a clear message referencing the spec requirement and/or ADR it advances. Keep the working tree coherent at every commit — never commit a knowingly broken intermediate state without saying so.
3. Stay within plan scope. If you discover the plan was incomplete, pause, update the plan (and ADR if a locked decision is affected), then continue.
4. Hold the line from Stage 1's Cut list: don't reintroduce cut scope mid-implementation. Before finishing, re-read the diff and delete anything not exercised by the feature's job — unused options, dead branches, speculative hooks.

## Stage 5 — Teeth check (adversarial)

1. Invoke the teeth skill against the implemented feature. Follow its phases and produce its verdict.
2. If teeth returns BLOCK or surfaces Critical/High findings, fix them (new commits), then re-run teeth on the fixes. Loop until teeth no longer blocks.
3. GATE: teeth verdict is PASS, or REVISE with only findings the user has explicitly accepted as out of scope.

## Stage 6 — Unit tests

1. Run the unit test suite for the touched code. Add unit tests for any new logic or edge case the spec or teeth surfaced that isn't covered.
2. GATE: unit tests pass. If you cannot run them, say so explicitly and give the exact command to run.

## Stage 7 — Integration tests

1. Run the integration tests covering the feature's end-to-end path. Add integration coverage for new cross-component flows.
2. GATE: integration tests pass (or the inability to run them is explicitly flagged with the command to run).

## Stage 8 — Smoke test against the live stack

1. Locate smoke scripts first: check whether smoke test scripts already exist in the repo for this feature / the affected services (look in the usual smoke/test locations, CI config, and any naming convention the project uses). If a relevant smoke script exists, use it. If none exists for this particular feature, say so explicitly and either write a minimal smoke script targeting the feature's critical path or flag that one is needed — do not silently skip smoke because a script is missing.
2. Bring up the live stack locally before smoke testing — the docker-compose / DevSpaces simulator that runs the actual clearing-to-risk services. Smoke tests MUST run against this running stack, NOT against mocks, stubs, or in-process fakes. If the stack is not already up, start it (or give the exact command to start it) and confirm the relevant services are healthy before proceeding.
3. Run the smoke test: a fast, shallow end-to-end check that the feature actually comes up on the live stack and performs its core happy-path function (the critical path the spec describes), exercising the real wired services rather than substitutes. Not exhaustive coverage.
4. Browser/E2E tests (aspirational): check whether browser-level end-to-end tests exist in the repo. If they do, run them against the same live stack (never against mocks). If they do not yet exist, explicitly note that the browser gate was skipped because no browser tests are present — do not fabricate a pass and do not silently omit it.
5. If the live stack cannot be started here, do not fake a pass: describe exactly how to bring it up and what to run, and mark this gate as "not run, action required."
6. GATE: smoke (and browser tests, if present) pass against the running live stack, or the inability to run them is explicitly flagged with the exact commands, which smoke scripts were found or created, and the stack/bring-up steps.

## Stage 9 — PR ready (STOP HERE)

1. Confirm: all commits are in place, teeth passed, unit + integration + smoke tests pass, ADRs are committed, and every spec acceptance item is satisfied.
2. Produce a PR-ready summary: the branch name, the base (main), a proposed PR title and description, the spec requirements covered, the ADRs added, and the test results.
3. Update the feature status tracker: append/update this feature's entry in feature-status-tracker.md at the repo root (create the file with a header row if it does not exist). Set status to PR-Raised. Each entry records: feature name, branch, base (main), spec file path, ADRs added, teeth verdict, unit/integration/smoke test status, date, and status. Use one entry per feature and update it in place across its lifecycle — do not create duplicate rows for the same feature. Commit this tracker update.
4. Give the user the exact commands to push and open the PR — but DO NOT run them. Pushing and opening the PR is the user's action.
5. STOP. Tell the user the branch is PR-ready and that you'll resume when they return with review comments.

## Stage 10 — Resume: resolve review comments (only when the user returns)

1. This stage runs only when the user comes back and pastes review comments.
2. Invoke the address-pr skill with the pasted comments. It verifies each comment against the local branch (diffing against main), triages (ACCEPT/PARTIAL/REJECT/STALE/CLARIFY), implements the accepted fixes, and reports.
3. After address-pr implements fixes, re-run the relevant gates: teeth on changed areas (Stage 5), then unit + integration tests (Stages 6-7), then smoke/browser against the live stack (Stage 8). Re-confirm spec acceptance.
4. Update the feature status tracker: set this feature's entry in feature-status-tracker.md to Comments-Resolved, refreshing the teeth verdict, test status, and date. Commit this tracker update.
5. Return to PR-ready state (Stage 9) with an updated summary of what changed in response to the comments.

## Operating rules across all stages

- The spec file wins over code, comments, and assumptions. Spec changes require the user's explicit say-so.
- Never push, open PRs, change remote settings, or perform other side-effectful remote actions. Stop and hand those to the user.
- A failed gate stops the pipeline. Surface the failure plainly; do not push past it to look complete.
- Smoke and browser tests run against the running live local stack, never against mocks or stubs. Do not report a pass for a stack that was never started.
- Be honest about anything you could not run or verify, and say exactly what the user should run.
- Keep commits small and traceable; keep the working tree coherent.
