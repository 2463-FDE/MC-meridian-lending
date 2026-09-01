# Handoff — implement the disclosure narration groundedness guard + offline judge (2026-08-31)

**Branch:** `docs/disclosure-narration-judge` · **Base:** `origin/main` · **Repo:**
`/Users/maha/Desktop/revature/MC-meridian-lending`
**Status:** D1 landed and proven. D2/D3/D4 not built. Spec is Draft (per its own Status —
D1 landing does not promote it; see spec Acceptance Criteria for what still gates that).

## What's done

- `docs/specs/disclosure-narration-judge.md` written and committed (`d3e7b39
  docs(disclosure): spec groundedness guard + offline judge for narration`).
- `scripts/spec_gate_map.txt` carries a real mapping line (not an `# EXEMPT:` line):
  `services/origination-service/app/disclosure_coordinator.py =>
  docs/specs/disclosure-narration-judge.md`.
- `docs/state.md` regenerated for merge-base drift (`chore(kb): regenerate docs/state.md
  for merge-base drift`) — the branch's base moved to `864147c` when #138 and #139 merged,
  so the committed page still named base `52723c1` and `kb-freshness` went red. Fix is
  `make kb` plus a commit, as always.
- **D1 — deterministic runtime guard.** `_narration_is_grounded`, wired into `_narrate`
  (`services/origination-service/app/disclosure_coordinator.py:561`), rejects a `summary`
  containing a currency amount, percent figure, or spelled-out number other than the
  exact `term_months`/`note_rate_pct` values given, degrading to `NARRATION_UNAVAILABLE`.
  Landed `5625671` (fix), unit-aware fix `0babb3e`, spec pinned `e7cfb5b`. Proven via
  `make prove`: `test_a_fabricated_dollar_figure_in_the_summary_is_rejected`
  (`tests/test_disclosure_coordinator.py`) fails without the guard, passes with it.
- `docs/debt-log.md` D37 opened (`bd2513b`) — Status: **Open, D1 landed, not yet
  Mitigated** (nothing blocking keeps the guard from regressing until D4 lands).
- Branch pushed: `origin/docs/disclosure-narration-judge`. No PR opened yet.
- Verified locally, both green: `bash scripts/spec_diff_gate.sh` and
  `scripts/check_doc_paths.sh` (the latter doesn't grade `docs/specs/*` — only
  README/CLAUDE.md/kb.md — but confirmed anyway).

## What's left

D1 is done (see above). D2–D4 remain, in spec order (each landable on its own):

1. **D2 — pinned fixture set.** ~12–15 synthetic `DisclosureState` cases: normal,
   unusually long term, minimum term, mismatched `checks_passed`. No real
   `application_id`/applicant data. No top-of-band-rate fixture — spec
   (`docs/specs/disclosure-narration-judge.md:147`) rules that axis not gradeable:
   `POLICY_RATE_PCT` is a single constant, no rate band exists to put a fixture at the top
   of.
2. **D3 — offline LLM judge.** Reuse `ClaudeClient` from
   `services/origination-service/app/llm/client.py` — no new provider dependency. Judge
   grades: (a) no figure beyond what the model was given, (b) `officer_action` matches the
   spec-defined `term_months > 60` cutoff only (`docs/specs/disclosure-narration-judge.md:174`)
   — the rate criterion is not graded, same reason as D2. Fail closed on judge error — same
   rule the reconciliation/atomic-apply gates hold.
3. **D4 — `disclosure-narration-gate`, blocking CI job.** Same placement pattern as
   `tila-vectors-gate` in `.github/workflows/ci.yml` — its own job, not under the
   tolerated `|| true`. **Not LangSmith.** The spec's CI contract for D4b (see
   `docs/specs/disclosure-narration-judge.md`, "CI contract for the judge-backed half")
   is explicit that `evaluate()` is rejected: it would add `LANGSMITH_API_KEY` and a
   hosted dataset the harness cannot grade or version, for no benefit — D2's fixtures are
   pinned files graded in-process. D4b instead needs `CLAUDE_PROVIDER`, `CLAUDE_MODEL`,
   and the provider credential pair (`app/llm/config.py`), provisioned as repo secrets,
   fails (never skips) on missing credentials, and does not run on a fork's `pull_request`
   (D4a still does, so nothing merges with the deterministic half unexercised).

Also required by the spec's Acceptance Criteria, not optional:
- D37 stays open (already carded, see above) until D1–D4 all land — do not close early.
- Add `disclosure-narration-gate` to the CI-gate list in `CLAUDE.md`.

## Blockers / open questions

- Spec's own **Open Questions** section, answered 2026-08-31: D1's guard logs the
  rejection event only, never the `summary` text (`disclosure_coordinator.py:594`) —
  avoids the PII-in-logs concern at the cost of the specific defect text for debugging.
- No PR opened yet. Decide whether to open one now (D1 landed, proven) or fold review
  into the eventual D2–D4 implementation PR.

## Key files

- `services/origination-service/app/disclosure_coordinator.py:561` — `_narrate`, where D1's
  guard is wired in (line 586).
- `services/origination-service/app/disclosure_coordinator.py:279` —
  `_narration_is_grounded`, D1's guard function.
- `services/origination-service/app/disclosure_coordinator.py:155` — `NARRATION_UNAVAILABLE`,
  the existing degrade path D1 reuses.
- `services/origination-service/app/prompts/disclosure_narrate.py` — the prompt/schema D1
  and D3 both reason about; `OUTPUT_SCHEMA` (line 16) validates shape only, not content.
- `rag_eval/evaluator.py:188` — the trace-content refusal that motivated *not* reusing
  `rag_eval`'s judge; read before assuming this judge inherits that constraint (it doesn't,
  because D2's data is synthetic).
- `docs/specs/disclosure-narration-judge.md` — the spec itself, source of truth for D1–D4.

## How to verify / run

- `bash scripts/spec_diff_gate.sh` — must stay green after any further doc changes.
- `cd services/origination-service && python -m pytest tests/test_disclosure_coordinator.py -q`
  — D1's regression test is in this file and passes on the current branch.
- `make prove` (D1's fix commit) — already run, PROVEN: fails without the guard, passes
  with it.
- Once D4 lands: confirm the new job appears as blocking (no `continue-on-error`) in
  `.github/workflows/ci.yml`, mirroring `tila-vectors-gate`'s structure.

## Branch state (cite the client's real baseline)

- `main` — disclosure stage 4b (`_narrate`) ships with schema validation only; a
  schema-valid, leak-guard-passing narration can currently state a fabricated APR or
  dollar figure with nothing to catch it. This is the client's real current gap.
- `docs/disclosure-narration-judge` — D1 landed (deterministic guard, proven). D2
  (fixtures), D3 (offline judge), D4 (blocking gate) not built.

## Debt log refs

- D37 (`docs/debt-log.md`) — Open, D1 landed, not yet Mitigated. Close only when D1–D4
  all merge and the gate is blocking, per the debt-log Mitigated/Fixed discipline.

## Next session: start here

Read `docs/specs/disclosure-narration-judge.md` in full (D1's section for what's already
built), then start D2: build the ~12–15 synthetic `DisclosureState` fixture set.
