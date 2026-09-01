# Handoff — implement the disclosure narration groundedness guard + offline judge (2026-08-31)

**Branch:** `docs/disclosure-narration-judge` · **Base:** `origin/main` · **Repo:**
`/Users/maha/Desktop/revature/MC-meridian-lending`
**Status:** Spec-only, pushed, no PR opened. Ready for implementation review before any
code lands — the spec itself is Draft and deliberately unbuilt.

## What's done

- `docs/specs/disclosure-narration-judge.md` written and committed (`d3e7b39
  docs(disclosure): spec groundedness guard + offline judge for narration`).
- `scripts/spec_gate_map.txt` updated with an `# EXEMPT:` line for the new spec
  (Status: Draft, no code path yet — same pattern as `rag-week2.md`'s exemption).
- `docs/state.md` regenerated for merge-base drift (`chore(kb): regenerate docs/state.md
  for merge-base drift`) — the branch's base moved to `864147c` when #138 and #139 merged,
  so the committed page still named base `52723c1` and `kb-freshness` went red. Fix is
  `make kb` plus a commit, as always.
- Branch pushed: `origin/docs/disclosure-narration-judge`. No PR opened yet.
- Verified locally, both green: `bash scripts/spec_diff_gate.sh` and
  `scripts/check_doc_paths.sh` (the latter doesn't grade `docs/specs/*` — only
  README/CLAUDE.md/kb.md — but confirmed anyway).

## What's left

Implement D1–D4 from the spec, in the order the spec gives (each is landable on its own):

1. **D1 — deterministic runtime guard.** Add a check in `_narrate`
   (`services/origination-service/app/disclosure_coordinator.py:413`) that rejects
   `summary` if it contains a currency amount, percent figure, or spelled-out number word
   other than the exact `term_months`/`note_rate_pct` values the model was given. On
   reject, degrade to `NARRATION_UNAVAILABLE` (already exists at line 153) via the same
   path validation/leak-guard failures use today (see the module docstring around line
   418 for the existing contract). Write the regression test first — a fixture `summary`
   with a fabricated dollar figure — and watch it fail before writing the guard (`make
   prove` convention, or hand-verify since this may not fit the pytest-only prove flow).
2. **D2 — pinned fixture set.** ~12–15 synthetic `DisclosureState` cases: normal,
   top-of-band rate, unusually long term, minimum term, mismatched `checks_passed`. No
   real `application_id`/applicant data.
3. **D3 — offline LLM judge.** Reuse `ClaudeClient` from
   `services/origination-service/app/llm/client.py` — no new provider dependency. Judge
   grades: (a) no figure beyond what the model was given, (b) `officer_action` matches
   the system prompt's own hold/send criteria (`app/prompts/disclosure_narrate.py:40-42`).
   Fail closed on judge error — same rule the reconciliation/atomic-apply gates hold.
4. **D4 — `disclosure-narration-gate`, blocking CI job.** Same placement pattern as
   `tila-vectors-gate` in `.github/workflows/ci.yml` — its own job, not under the
   tolerated `|| true`. Wire through LangSmith's `evaluate()` API — this is the one judge
   in the repo that can, since D2's fixtures are synthetic (no real-applicant trace
   content problem the `rag_eval` judge has, see spec's *Why not reuse rag_eval*).

Also required by the spec's Acceptance Criteria, not optional:
- New `docs/debt-log.md` entry (next free D-number — check the highest existing before
  assigning one) naming the gap while D1–D4 are in progress. Close it only once all four
  land, per the debt-log Mitigated/Fixed discipline in `CLAUDE.md`.
- Add `disclosure-narration-gate` to the CI-gate list in `CLAUDE.md`.

## Blockers / open questions

- Spec's own **Open Questions** section: should D1's guard log the rejected `summary`
  text for audit, or only the rejection event? Logging the text risks a PII-in-logs
  concern (the redactor pattern exists for exactly this); logging only the event loses
  the specific defect for debugging. Needs an answer before D1 lands — ask the user, not
  a judgment call to make silently.
- No PR opened for the spec itself yet. Decide whether to open one for review before
  starting D1, or fold spec review into the eventual implementation PR.

## Key files

- `services/origination-service/app/disclosure_coordinator.py:413` — `_narrate`, where D1's
  guard attaches.
- `services/origination-service/app/disclosure_coordinator.py:153` — `NARRATION_UNAVAILABLE`,
  the existing degrade path D1 reuses.
- `services/origination-service/app/prompts/disclosure_narrate.py` — the prompt/schema D1
  and D3 both reason about; `OUTPUT_SCHEMA` (line 16) validates shape only, not content.
- `rag_eval/evaluator.py:188` — the trace-content refusal that motivated *not* reusing
  `rag_eval`'s judge; read before assuming this judge inherits that constraint (it doesn't,
  because D2's data is synthetic).
- `docs/specs/disclosure-narration-judge.md` — the spec itself, source of truth for D1–D4.

## How to verify / run

- `bash scripts/spec_diff_gate.sh` — must stay green after any further doc changes.
- `cd services/origination-service && python -m pytest -q` — baseline before touching
  `disclosure_coordinator.py`; not run this session since no code changed yet.
- Once D1 lands: `cd services/origination-service && python -m pytest
  tests/test_disclosure_coordinator.py -q` plus the new regression test.
- Once D4 lands: confirm the new job appears as blocking (no `continue-on-error`) in
  `.github/workflows/ci.yml`, mirroring `tila-vectors-gate`'s structure.

## Branch state (cite the client's real baseline)

- `main` — disclosure stage 4b (`_narrate`) ships with schema validation only; a
  schema-valid, leak-guard-passing narration can currently state a fabricated APR or
  dollar figure with nothing to catch it. This is the client's real current gap.
- `docs/disclosure-narration-judge` — spec only, no code. Implementation not started.

## Debt log refs

- No debt-log entry exists yet for this gap — spec's Acceptance Criteria requires one be
  added (next free D-number) when implementation starts, closed only when D1–D4 all merge.

## Next session: start here

Read `docs/specs/disclosure-narration-judge.md` in full, then start D1: write the failing
regression test for a fabricated-figure `summary` in
`services/origination-service/tests/test_disclosure_coordinator.py` before touching
`disclosure_coordinator.py:413`.
