# Policy retrieval evaluation — results

> **Where the inputs live.** The policy packet, the displayed-summaries package,
> the gold set and the client-facing correspondence are NOT in this repository and
> must not be added to it: this is a public fork, and the client's approval covers
> indexing her material, not publishing it. Paths to them appear below as
> `<packet>`, `<summaries>` and `<corpus dir>`. This document records the method,
> the decisions and the measured results, which is what a future session needs.


**Date:** 2026-08-30
**Corpus:** the policy packet supplied 2026-08-27, 18 files, verified against its
own SHA-256 manifest before and after the run
**Scope:** one bounded evaluation pass over the 28 officer questions, graded on
three targets

---

## 1. What was measured, and what it cost

The evaluation indexes the supplied policy documents, runs each of the 28 officer
questions through retrieval, and grades the result on three separate targets. It
was run once. Every question is now spent, and the numbers below cannot be
re-measured on this gold set.

| | |
|---|---|
| Candidate files scanned | 6, 0 refused by the content and filename checks. This is the hygiene gate's file count, not a document count: only gate-passed markdown reaches the chunker, and the 66 passages below match the five-document measurement recorded on 2026-08-27, so the sixth candidate contributed none |
| Passages indexed | 66 |
| Embedding model | `amazon.titan-embed-text-v2:0`, 1024 dimensions |
| Embedding calls | 94 (66 passages + 28 questions), 0 retries, 6,644 input tokens |
| Grading model | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| Grading calls | 28, one per question |
| Calling region | `us-east-1` |

**On region:** the `us.` prefix denotes a cross-region inference profile. `us-east-1`
is accurate as the region the call was *made to*. It is not a data-residency
statement, and we are not making one.

**On retention:** no prompt, passage or model response is retained anywhere. The
embedding cache is disabled on the provider path, so nothing derived from the
content is written to disk. External tracing was excluded and is enforced in code —
the evaluator refuses to start if tracing is enabled, checked before any call is
made.

---

## 2. Retrieval

Of the 28 questions: 17 are answerable from the corpus, 6 are deliberately
unanswerable, and 5 are clarification cases that are ambiguous across documents by
design and are scored on nothing.

**Answerable questions (17):** hit@1 = 0.71 · hit@3 = 0.88 · hit@5 = 0.94 · MRR = 0.80

**Unanswerable questions (6):** all 6 correctly fell below the confidence
threshold — the system declined rather than answering from material that cannot
support an answer.

### The confidence threshold

**0.3007877649147387**, derived from this run.

The threshold belongs to exactly one pairing — this corpus and this embedding
model — and must be re-derived if either changes. At this value, 2 answerable
questions would be declined that could have been answered, and **0 unanswerable
questions are answered with false confidence.**

That trade is deliberate. Declining a question the corpus can answer costs an
officer a second look. Answering confidently from a corpus that cannot support the
answer puts a wrong statement in front of an officer with no signal that it is
wrong. We accepted two of the first to eliminate all of the second.

### Abstention behaviour — the result we would lead with

We ran a keyword-based retrieval baseline over the identical corpus on the same day,
each method at its own calibrated threshold. The two differ in *which* error they
make, not in how many:

| | Keyword baseline | Embedding model (this pass) |
|---|---|---|
| Answerable questions wrongly declined | 0 | 2 |
| **Unanswerable questions answered with false confidence** | **5** | **0** |

The baseline never declines a question it could answer — and answers five it should
have refused. The embedding model reverses that completely.

For an officer-facing tool we regard this as the more important of the two results
above. A wrongly declined question costs an officer a second look and shows them
nothing untrue. A confidently retrieved answer to a question the corpus cannot
support puts a wrong statement in front of an officer with nothing marking it as
wrong — and the officer has no way to tell the two situations apart.

We accepted two of the first to eliminate all five of the second. The threshold is the
single number that moves that trade-off, and it can be re-derived against a stated
preference from the retrieval scores this run already captured — that costs no new
measurement and spends no question. What it cannot do is restate the rates in section 3
under a different threshold. Those were graded once against this gold set, every question
is now spent, and rates under a new threshold would require a fresh gold set and a fresh
pass.

---

## 3. Grading results

Three targets are graded per question and **the rates are never combined.** Each
answers a different question, and a single blended number would hide which one
moved.

| Target | Graded | Rate | Sent to human review |
|---|---|---|---|
| Expected conclusion — does the retrieved policy support it? | 25 | **1.00** | 3 |
| Displayed summary — is what the officer is shown supported? | 23 | **1.00** | 5 |
| Prohibited conclusion — did retrieval stay away from it? | 23 | **1.00** (avoided) | 5 |

Questions sent to human review count in neither the numerator nor the denominator.
They are not counted as failures and they are not counted as passes.

The prohibited column scores *avoided* — a high number means retrieval stayed off
the conclusion identified as unacceptable.

### By topic

| Topic | Questions | Expected conclusion | Displayed summary | Prohibited |
|---|---|---|---|---|
| Adverse action | 8 | 1.00 (7 graded) | 1.00 (6) | 1.00 (6) |
| Credit decisioning | 5 | 1.00 (5) | 1.00 (5) | 1.00 (5) |
| Records retention | 4 | 1.00 (3) | 1.00 (2) | 1.00 (2) |
| Eligibility rules | 7 | 1.00 (6) | 1.00 (6) | 1.00 (6) |
| Debt to income | 1 | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| Fee schedule | 1 | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| Interest rate | 1 | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| APR / finance charge | 1 | 1.00 (1) | 1.00 (1) | 1.00 (1) |

### How the four literal-backed questions were graded

Four questions carry a required literal — "30 days", "12 CFR 1002.9", "25 months",
"36 percent". For those, the **expected-conclusion** target is checked mechanically
against the retrieved text and spends no model call. The displayed-summary and
prohibited targets on those same questions still go to the model, because neither
has a mechanical equivalent.

This is the narrower of the two available readings, and it is the one that keeps
all eight topics in scope. The wider reading would have removed the only APR /
finance-charge question from grading entirely and left that topic blank on all
three targets.

---

## 4. What we are telling you before you ask

These are limitations of this pass. None is hidden in the numbers above.

### 4.1 The 13 human-review results are a length limit, not uncertainty

This is the most important qualification in this report.

The evaluator returns a one-line rationale with each verdict, capped at 240
characters because we do not retain model text beyond one line. When the model
exceeded that limit, the system **did not truncate** — truncating would still have
retained 240 characters of your policy text. It discarded the model's verdicts for
that question and marked them for human review instead.

Five questions hit this: **q01, q04, q13, q14, q16.** For q01 and q04 the expected
conclusion survived, because it was graded mechanically against the required
literal rather than by the model — which is why the three targets show 3, 5 and 5
rather than 5, 5 and 5.

The model writes close to the limit throughout: across the 23 questions that did
grade, the shortest rationale is 125 characters, the median 189, and the longest
exactly 240. Three more (q06, q07, q24) came within 20 characters of it.

**So the human-review bucket says something about our character limit, not about
your corpus and not about the model's confidence.** Reading it as "the evaluator
was unsure about five questions" would be wrong. Raising the limit or asking for a
shorter rationale would recover them, but not on this pass — the questions are
spent.

### 4.2 There is no negative control in this pass

Every graded verdict on every target came back positive. There is no question in
this set with a deliberately wrong expected conclusion, so **this pass does not
demonstrate that the grader can return a negative verdict.** A 1.00 across three
targets should be read with that in mind. Adding a control case is the first thing
we would change about the gold set.

### 4.3 Four topics rest on a single question each

Debt to income, fee schedule, interest rate and APR / finance charge have one
question apiece. If that question had gone to human review, the topic would have
reported no rate at all rather than a low one. It did not happen here — all four
graded — but the rates for those four topics carry the weight of one observation.

### 4.4 The grading model does not follow its output instruction exactly

The model was instructed to return a bare JSON object and returns it wrapped in a
markdown code fence instead. This is handled and affects no result. We note it
because it is an observed behaviour of the model under a fixed instruction, and it
is the kind of thing that matters if the evaluator is ever pointed at a different
model.

---

## 5. What this does and does not tell you

**It tells you** that on this corpus, retrieval finds the right passage first for
roughly seven questions in ten and within the top three for nearly nine in ten;
that it correctly declines every question the corpus cannot answer; and that where
the model graded, it found the retrieved policy supported the expected conclusion,
supported what the officer is shown, and stayed away from the prohibited
conclusion.

**It does not tell you** how the system behaves on questions outside these 28, on
policy documents outside this packet, or on a corpus large enough to change the
threshold. Nor does it establish, from this pass alone, that the grader
discriminates — see 4.2.

## 6. Recommended next steps

1. **Add negative controls to the gold set** — questions with a knowingly wrong
   expected conclusion, so a future pass demonstrates the grader returns negatives.
2. **Settle the rationale length limit** — either raise it or shorten what is asked
   for, so results are not lost to it. Five of 28 is too many.
3. **Add questions to the four single-question topics**, so no topic's rate rests
   on one observation.
4. **Re-derive the threshold** whenever the corpus or the embedding model changes.
   It is bound to this pairing and is not portable.
