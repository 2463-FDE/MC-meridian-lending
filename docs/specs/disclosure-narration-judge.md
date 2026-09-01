# Spec: Groundedness Guard + Offline Judge for Disclosure Narration

**Owner:** maha-c
**Date:** 2026-08-31
**Status:** Draft — not built. Not approved for build; this is the design record to review
before opening an implementation PR.
**Governs:** `services/origination-service/app/disclosure_coordinator.py` stage 4b
(`_narrate`) and `services/origination-service/app/prompts/disclosure_narrate.py`, once
implemented.
**Related:** ADR 0012 (Decimal/minor units, the maker-checker pipeline this stage belongs
to), `docs/specs/disclosure-week4.md` (D4/D5, the deterministic gate this stage runs
behind), `rag_eval/evaluator.py` (the only other LLM-judge in this repo — precedent and
contrast, see *Why not reuse rag_eval*).

---

## Problem Statement

### Verified in code, 2026-08-31

Disclosure stage 4a (`_verify`, `disclosure_coordinator.py:398`) is a deterministic gate: it
recomputes the document's rendered figures and compares them to `state["figures"]`
field-by-field. It is the only stage that can fail the run, and it holds. That part of the
pipeline is not the gap.

Stage 4b (`_narrate`, `disclosure_coordinator.py:413`) runs only after 4a passes, and writes
the officer-facing brief. By design it is given none of the five disclosed figures
(`FIGURE_FIELDS`: `apr`, `finance_charge`, `amount_financed`, `total_of_payments`,
`monthly_payment`). Its prompt (`prompts/disclosure_narrate.py`) passes exactly
`application_id`, `term_months`, `note_rate_pct`, `checks_passed`, and its system prompt
says outright:

> "You are not the check. Do not re-derive, question, or comment on the accuracy of any
> number — you have not been given the inputs to do so."

The stated reason (module docstring, `disclosure_coordinator.py:8`) is sound: a model asked
to verify a regulated number can be wrong in the permissive direction, so the fix was to
remove its ability to check a number at all. What that fix does not address: nothing stops
the model from **stating** a number it invents. `OUTPUT_SCHEMA`
(`prompts/disclosure_narrate.py:16`) constrains `summary` to `type: string, maxLength: 500`
— it validates shape, not content. A schema-valid, leak-guard-passing `summary` can contain
"...at 12.4% APR..." or "...your monthly payment of $340..." with no relationship to the
real `apr`/`monthly_payment` in `state["figures"]`, and nothing in the pipeline would catch
it before it reaches the officer. `NARRATION_UNAVAILABLE`
(`disclosure_coordinator.py:153`) already exists as a degrade path for "validation and
leak-guard rejections" (line 418) — a fabricated figure is a new rejection reason that
degrade path does not yet cover.

This is the same shape as the D2 add-on-vs-actuarial APR defect
(`docs/specs/disclosure-week4.md`): a wrong number reaching the borrower/officer path with
no test exercising the failure mode. That one shipped because the money test ran under
`|| true`. This one has never shipped a defect (no report of a fabricated figure in a live
narration) — the gap is that nothing would notice if it did.

### Why not reuse rag_eval

`rag_eval/evaluator.py` already has a Bedrock LLM-judge (Titan, graded-pass 2026-08-30).
Two reasons it isn't the vehicle here:

1. **Different content-safety posture.** `rag_eval`'s judge grades officer policy-question
   answers, which can carry live applicant context, so it explicitly refuses to run under
   `LANGSMITH_TRACING` (`evaluator.py:188`) — trace error bodies aren't hardened for it the
   way `origination-service/app/llm/client.py`'s `process_inputs`/`process_outputs` are.
   This judge grades **synthetic fixture states only** (see D2 below) — no real applicant
   ever enters it — so that restriction doesn't apply, and it's the one place in this repo
   that can wire LangSmith's `evaluate()` API against a judge without first doing the
   trace-content hardening `rag_eval` deliberately declined.
2. **Different question.** `rag_eval` grades retrieval + answer faithfulness against a
   corpus. This grades whether a fixed-shape briefing paragraph invents figures it was never
   given. Different judge prompt, different dataset, different failure mode — folding it
   into `rag_eval` would make one module answer two unrelated questions.

## Options Considered

1. **LLM judge only, runtime, on every disclosure.** Rejected: adds a second model call
   (latency + cost) to the money path for every approved application, and an LLM judge is
   itself non-deterministic — using one to guard a stage that exists specifically because a
   *first* model call couldn't be trusted with figures is circular. If the judge is wrong in
   the permissive direction, nothing catches it either.
2. **Deterministic guard only, no judge.** Cheapest, but a regex over `summary` for
   `$`/`%`/bare numbers catches literal digits, not "twelve point four percent" or "one
   month's payment" spelled out in words — a model asked not to state a number can still
   gesture at one. Rejected alone as insufficient, but see D1 below: it is the right runtime
   layer, just not the only layer.
3. **Deterministic guard at runtime + LLM judge offline, as a blocking CI gate over pinned
   fixtures — chosen.** Mirrors the existing `tila-vectors-gate`/`atomic-apply-gate`
   pattern: a cheap deterministic check stays in the request path, and the judge — better at
   catching spelled-out or paraphrased figures than a regex, but too slow/nondeterministic
   for a live request — runs offline against a small pinned dataset and blocks the merge if
   the checker prompt regresses. No added runtime latency or cost; regressions are caught
   before merge instead of after a live narration ships one.

## Minimum Build Slice

1. **Deterministic numeric-leak guard, runtime** (D1). A pure-Python check on `summary`
   before it's returned from `_narrate`: reject if it contains a currency amount, a percent
   figure, or a spelled-out number word, unless that figure exactly equals `term_months` or
   `note_rate_pct` (the two numbers the model was actually given). On reject, degrade to
   `NARRATION_UNAVAILABLE` via the same path validation/leak-guard failures already use
   (`disclosure_coordinator.py:418`) — no new failure mode, one new rejection reason. This
   alone is landable and testable without the judge below.
2. **Pinned fixture set** (D2). ~12–15 synthetic `DisclosureState` inputs spanning: normal
   term/rate, top-of-band rate (should route `hold_for_compliance` per the system prompt's
   own instruction), unusually long term, minimum term, a `checks_passed` count that
   doesn't match `len(FIGURE_FIELDS)` (shouldn't reach this stage per stage 4a, but the
   fixture exercises it defensively). No real `application_id` or applicant data — synthetic
   IDs only, same convention as the TILA test vectors.
3. **Offline LLM judge** (D3). Reuses `ClaudeClient` (`app/llm/client.py`) already in the
   service — no new provider dependency. Judge prompt grades each fixture's `_narrate`
   output on two axes: (a) contains no figure beyond `term_months`/`note_rate_pct` — this is
   a second, catch-what-regex-misses pass over the same question D1 answers at runtime, not
   a different question; (b) `officer_action` matches what the system prompt's own criteria
   would pick for that fixture (top-of-band rate / unusually long term →
   `hold_for_compliance`). Fails closed: a judge call that errors or returns unparseable
   output counts as a fixture failure, not a skip — same rule the reconciliation and
   atomic-apply gates hold on their own inputs.
4. **`disclosure-narration-gate`, blocking CI job** (D4). Runs D2 fixtures through D3 outside
   the matrix (i.e., not under the tolerated `|| true`), same placement as
   `tila-vectors-gate`. Wired through LangSmith's `evaluate()` API against the fixture
   dataset (see *Why not reuse rag_eval* — this is the one judge in the repo positioned to
   use it directly). A judge-prompt or `disclosure_narrate` prompt change that regresses
   grounding or action-selection fails the gate before merge.

## Out of Scope

- Judging `_assemble` (stage 3, the maker). It's handed the exact figures and told to copy
  them; stage 4a already catches a copy error deterministically. A judge there would grade
  what a diff already grades for free.
- A runtime LLM judge call (see Options, #1).
- Extending this judge to other LLM surfaces (officer assistant final-answer groundedness,
  `search_policy` citation grounding). Real gaps, but a separate judge against a separate
  dataset — bundling them here would make the fixture set answer two unrelated questions,
  the same reason `rag_eval` wasn't reused.

## Acceptance Criteria

- A fixture narration containing a fabricated dollar or percent figure is rejected by D1 at
  runtime (regression test: assert the exact fixture that motivated this spec — a `summary`
  stating an invented monthly payment — degrades to `NARRATION_UNAVAILABLE`, not silently
  passes).
- `disclosure-narration-gate` is blocking (no `continue-on-error`, no `|| true`), fails on a
  fixture the judge marks fabricated or misrouted, and is added to the CI-gate list in
  `CLAUDE.md`.
- `docs/debt-log.md` gets a new entry (next available D-number) naming this gap while it's
  open, closed only once D1–D4 are all merged — per the debt-log status discipline
  (Mitigated requires something blocking; do not mark Fixed with any row still open).

## Risks

- **Judge portability.** `rag_eval`'s graded-pass work found a judge threshold does not
  transfer across corpus or embedder without re-validation — the same caution applies here:
  this judge's fixture set and pass criteria are specific to `disclosure_narrate`'s prompt
  shape and need re-validation, not just a re-run, if that prompt changes materially.
- **Fixture staleness.** If `prompts/disclosure_narrate.py`'s `required_vars` or
  `OUTPUT_SCHEMA` change, D2's fixtures and D3's judge prompt both need review — the gate
  would otherwise grade a stage that no longer matches what it's testing.
- **Regex false negatives (D1 alone).** Named in Options #2 — mitigated by D3 running the
  same question through a judge offline, not by strengthening the regex indefinitely.

## Open Questions

- Should D1's guard log the rejected `summary` text for audit, or only the rejection event?
  Logging the text risks the same PII-in-logs concern the redactor exists for; logging only
  the event loses the specific defect for debugging. Needs an answer before D1 lands, not
  during review.
