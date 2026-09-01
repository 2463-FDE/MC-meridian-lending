# Spec: Groundedness Guard + Offline Judge for Disclosure Narration

**Owner:** maha-c
**Date:** 2026-08-31
**Status:** Partly built. D1 (the runtime groundedness guard) is implemented and covered
by tests in commit `5625671` on this document's own branch — `_narration_is_grounded` in
`services/origination-service/app/disclosure_coordinator.py`, wired into `_narrate`. D2, D3
and D4 are designed here and not built. "Built" in this document means implemented and
reviewed on the branch that carries this file, not present in `main`; confirm with
`git merge-base --is-ancestor <branch> main` before citing it elsewhere.
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
(`prompts/disclosure_narrate.py:16`) declares `summary` as `type: string, maxLength: 500`,
and the validator enforces neither the length nor the content: `pattern` is the only string
facet it checks, and its own comment records that `maxLength` "is declared by several
prompts and is NOT enforced" (`app/llm/validator.py`). So the schema constrains the type
and nothing else. A schema-valid, leak-guard-passing `summary` can contain
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
   before it's returned from `_narrate`. The rule is **unit-aware, not value-only** — a
   value-only allowed set passes the exact figure the guard exists to catch, because at
   `term_months` 48 a fabricated "$48.00 monthly payment" is a dollar amount the model was
   never given whose value is nonetheless on the list. Three unit classes:

   | Unit class | Matched by | Allowed values |
   |---|---|---|
   | money | a `$` amount, or a figure followed by `dollars`/`cents`/`payment(s)` | none — `_narrate` is handed no dollar amount at all |
   | rate | a figure followed by `%`, `percent`, `apr` or `rate` | `note_rate_pct` only |
   | term | a figure followed by `month(s)` | `term_months` only |

   A figure carrying no unit word is not graded: `application_id` and `checks_passed` are
   both given to the model and both appear as bare integers.

   Spelled-out numbers are read only when **directly adjacent to a unit word**. That
   adjacency requirement is what keeps ordinary prose out of the guard — the prompt's own
   `"<one or two sentences for the officer>"` instruction puts neither word next to a unit,
   so compliant prose does not trip it. The recognised tokens are `zero`–`twenty` plus
   `thirty`, `forty`, `fifty`, `sixty`, `seventy`, `eighty`, `ninety`; at most two
   whole-number words combine and they sum (`twenty five` → 25). An optional `point` tail
   makes a decimal: words after `point` are digits in order, so `seven point nine nine
   percent` normalizes to `7.99` and compares equal to `note_rate_pct`. Without that rule
   the words sum to 18 and a **truthful** spelled rate degrades the officer's brief on the
   money path. Nothing above `ninety-nine` is recognised (no `hundred`/`thousand`) — a
   deliberate stopping point, per Risks below.

   On reject, degrade to `NARRATION_UNAVAILABLE` via the same path validation/leak-guard
   failures already use — no new failure mode, one new rejection reason
   (`reason=ungrounded_figure` in the degrade log line). This alone is landable and testable
   without the judge below, and is what commit `5625671` implements.
2. **Pinned fixture set** (D2). ~12–15 synthetic `DisclosureState` inputs spanning: normal
   term/rate, unusually long term, minimum term, a `checks_passed` count that doesn't match
   `len(FIGURE_FIELDS)` (shouldn't reach this stage per stage 4a, but the fixture exercises
   it defensively). No real `application_id` or applicant data — synthetic IDs only, same
   convention as the TILA test vectors.

   **Thresholds, and why one of the prompt's two criteria is not gradeable.** The system
   prompt asks for `hold_for_compliance` on "an unusually long term, a rate at the top of
   the band" (`prompts/disclosure_narrate.py`) and defines neither. Fixture expectations
   cannot be one author's guess, so:

   - **Long term:** `term_months > 60` routes `hold_for_compliance`. This cutoff is defined
     by this spec, not by a policy source — no term band exists anywhere in the repo. If one
     is later added under `policies/`, this cutoff moves there and the fixtures cite it.
   - **Top-of-band rate: not gradeable, and D3 does not grade it.** `policies/fee_schedule.md`
     documents a band (7.99%–24.99% APR by risk band), but no code enforces it:
     `POLICY_RATE_PCT = 7.99` (`services/origination-service/app/routers/offers.py`) is a
     single constant applied to every offer, so no fixture can present a rate at the top of
     a band the code never produces. Grading this axis would pin a boundary runtime behavior
     cannot reach. The axis is deferred until the band is code-enforced; when it is, this
     section and D3's axis (b) are what change.
3. **Offline LLM judge** (D3). Reuses `ClaudeClient` (`app/llm/client.py`) already in the
   service — no new provider dependency.

   **The graded artifact is the raw `disclosure_narrate` completion, before D1 runs.** This
   is the decision that determines whether the gate has teeth. D1 replaces an ungrounded
   `summary` with `NARRATION_UNAVAILABLE` and discards the text, so a judge reading
   `_narrate`'s return value can only ever see a grounded summary or the canned brief — it
   would pass every fixture whether or not the checker prompt had regressed, a blocking gate
   proving nothing. The D3 harness therefore drives the prompt directly rather than calling
   `_narrate`, grading the model's own completion. This requires **no change to D1's return
   shape**: `_narrate` keeps discarding the rejected text at runtime (see Answered
   Questions), and the raw completion exists only inside the offline harness, over synthetic
   fixtures.

   Three axes per fixture:

   - **(a) Groundedness.** The raw completion states no figure beyond `term_months` and
     `note_rate_pct`, under the unit rules in D1. This is a second, catch-what-regex-misses
     pass over the same question D1 answers at runtime — spelled-out or paraphrased figures
     a regex cannot enumerate.
   - **(b) `officer_action`.** Graded against the D2 term cutoff only. The rate criterion is
     not graded — see D2, the band is documented but code-unenforced.
   - **(c) Agreement with D1.** D1's verdict on that same raw completion must match axis
     (a). A completion the judge marks fabricated that D1 passed is a gate failure: that
     disagreement is the regex hole D1's own Risks section predicts, and catching it is why
     the judge grades the pre-guard text.

   Fails closed: a judge call that errors or returns unparseable output counts as a fixture
   failure, not a skip — same rule the reconciliation and atomic-apply gates hold on their
   own inputs.
4. **`disclosure-narration-gate`, blocking CI job** (D4). Runs outside the matrix (i.e.,
   not under the tolerated `|| true`), same placement as `tila-vectors-gate`. It has two
   halves, because every other blocking job in `ci.yml` is keyless and this one cannot be:

   - **D4a, keyless and always blocking.** Runs the D2 fixtures through D1's guard and
     asserts each fixture's recorded verdict, plus the D1 unit tests. No provider call, no
     secret, runs on every pull request including a fork's. This is the half that holds the
     shipped control.
   - **D4b, judge-backed.** Runs D3 over the same fixtures. Requires credentials, so it is
     governed by the contract below.

   A judge-prompt or `disclosure_narrate` prompt change that regresses grounding or
   action-selection fails the gate before merge.

### CI contract for the judge-backed half (D4b)

`ci.yml` currently contains **no** job that reads a secret — every blocking gate is offline
and grades text, SQL or Python. D4b is the first exception, so its prerequisites are stated
here rather than inherited:

| Item | Value |
|---|---|
| Required env | `CLAUDE_PROVIDER`, `CLAUDE_MODEL`, and the provider's credential — `AWS_REGION` plus `AWS_BEARER_TOKEN_BEDROCK` on the bedrock path, `CLAUDE_API_KEY` on the anthropic path (`app/llm/config.py` validates the pair) |
| Provisioning | Repository secrets on this repository, set by the repo owner. Not organisation-wide, not inherited |
| Missing credentials | **Fail, never skip.** The job asserts its env is populated before the first fixture and exits non-zero if not, matching the `REQUIRE_LIVE_DB` rule `no-sad-gate` and `assistant-telemetry-gate` already use for a datastore they cannot reach |
| Fork pull requests | GitHub withholds secrets from a fork's pull request. D4b therefore does not run on `pull_request` from a fork; it runs on `pull_request` within this repository and on `workflow_dispatch`. D4a still blocks a fork's pull request, so nothing merges with the control unexercised |
| LangSmith | Not used. `evaluate()` would add `LANGSMITH_API_KEY` and a hosted dataset to the same job's prerequisites for no grading the harness cannot do locally, and a hosted dataset is state the repo cannot version. The fixtures are pinned files, graded in-process |
| Trace content | The judge runs over synthetic fixtures only, so `rag_eval`'s refusal to run under `LANGSMITH_TRACING` (`evaluator.py:188`) does not bind here — but D4b sets no tracing env either, so the question does not arise |

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
- `disclosure-narration-gate` is blocking (no `continue-on-error`, no `|| true`), and is
  added to the CI-gate list in `CLAUDE.md`. D4a fails on a fixture whose D1 verdict does not
  match its recorded expectation; D4b fails on a fixture the judge marks fabricated or
  misrouted, on a D1/judge disagreement (D3 axis (c)), and on absent credentials.
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

## Answered Questions

- **Does D1's guard log the rejected `summary` text, or only the rejection event?** Answered
  2026-08-31, before D1 landed: **the event only.** The degrade line carries
  `reason=ungrounded_figure` and the `application_id`, never the text. The text that trips
  this guard is model-authored prose about a live application — exactly what the PII
  redactor and the leak guard exist to keep out of logs — and the debugging value it would
  add is recoverable offline from the D2 fixtures, which carry no applicant data. See
  `_narrate` in `disclosure_coordinator.py`.
- **Which artifact does D3 grade?** Answered in D3 above: the raw `disclosure_narrate`
  completion, before D1 runs. Grading post-D1 output makes the gate vacuous.
