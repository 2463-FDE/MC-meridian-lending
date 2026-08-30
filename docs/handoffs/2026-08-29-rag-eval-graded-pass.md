# Handoff — RAG eval, the graded Titan pass

> **Where the inputs live.** The policy packet, the displayed-summaries package,
> the gold set and the client-facing correspondence are NOT in this repository and
> must not be added to it: this is a public fork, and the client's approval covers
> indexing her material, not publishing it. Paths to them appear below as
> `<packet>`, `<summaries>` and `<corpus dir>`. This document records the method,
> the decisions and the measured results, which is what a future session needs.


**Written 2026-08-29.** Lives in the corpus directory's own local git repository,
never in `MC-meridian-lending` — that is a PUBLIC fork of
`2463-FDE/meridian-lending`, and this file carries her packet paths, her acceptance
item wording and the `support_literal` values from her gold set. A local-only branch
there was rejected: the objects would still sit in a clone whose `origin` is that
fork, and one stray push refspec publishes them irrecoverably. That repository has no
remote and must not be given one.

Supersedes `docs/handoffs/2026-08-28-rag-eval-titan-pass.md` for sequencing; that file keeps the
setup detail (steps 1–10) and `docs/rag-eval-run-pipeline.md` keeps the pipeline. Both are
siblings in `handoffs/`.

**Operational companion:** ``docs/runbook-rag-eval-graded-pass.md``, beside the scripts it
documents — exact commands, expected output, stop conditions. This file is the why;
that one is the how.

## STATUS: EXECUTED 2026-08-30 — this file is now history

The pass ran on 2026-08-30. Everything below describes the plan as it stood before
it, and is kept for the reasoning, not the state. **The record of what actually
happened is `docs/handoffs/2026-08-30-rag-eval-graded-pass-run-record.md`**, and the deliverable written
from it is `docs/rag-eval-graded-pass-report-2026-08-30.md`.

What changed against the plan below:

- **PR #116 merged** (`64f4871`), so `filename-pii` is on `main`. #117 and #119
  followed; `origin/main` = `e5f6ea7`.
- **A third build item appeared during Phase B and had to merge first.** Haiku 4.5
  wraps its JSON in a markdown fence, so `grade()`'s bare `json.loads` raised and
  returned all three axes at `human_review` — on the pass that would have fired on
  all 28 cases. Fixed in **PR #119**, `_first_json_object()`. Section 2's claim that
  the fake rehearsal plus one smoke call was sufficient preparation held: the smoke
  is what caught it.
- **The single-case-topic risk did not fire.** All four one-case topics graded.
- **A risk this file did not anticipate did fire:** the 240-character rationale cap
  downgraded five cases to `human_review`. See the run record, section 7.

## State

- `origin/main` = `7399e8a`. Merged 2026-08-29: #112 (policy corpus manifest paths),
  #113 (assistant rate limit), #114 (the evaluator wiring), #115 (summary provenance).
- **PR #116 `fix/rag-eval-acceptance-fixtures` is the only thing open.** Commit
  `7fe277c`, 365 tests, merges clean against `7399e8a`. It is the last build item
  before the pass.
- Everything else is on `main`: `support_literal` in the gold set, the
  verdict/rationale carriers, the evaluator, ADR 0022, and the wiring.

Verify rather than trust this block — it decays every time something merges:

    git rev-parse --short origin/main
    curl -s https://api.github.com/repos/2463-FDE/MC-meridian-lending/pulls?state=open

**The branch moved after the first push.** Three review-round commits landed from a
parallel session, and all three are valid:

- `930bdd3` — removes the call-site rationale truncation. Truncating to
  `RATIONALE_MAX_CHARS` still persisted model text that `QueryEval.__post_init__`
  exists to reject; the guard was being defeated upstream. An overlong rationale now
  downgrades the model-derived axes to `human_review` with a content-free string.
- `968f046` — a gold row with no support-test targets was still calling
  `evaluator.grade()` with empty strings and recording the replies as real grades.
  Each axis is now held at `not_evaluated` unless its own target field is present,
  and `grade()` is skipped entirely when no axis on the row has a target.
- `c488dd2` — the report's "zero LLM calls" sentence was unconditional and would have
  been false on a judged run. It now states backend, model id and call count.

## Merge #114, then the sequence below

### 1. The two unmeasured acceptance items — do this first, it is free

Her acceptance set is **30, not 28**: the 28 officer questions plus 2 whole-document
exclusions in `acceptance/no-borrower-data-boundary.jsonl`.

| id | requires |
|----|----------|
| `FIX-NEG-BORROWER-CONTENT` | body carries borrower-data-like fields → exclude the whole document, not redact-and-keep |
| `FIX-NEG-BORROWER-FILENAME` | filename alone is borrower-data-like → exclude even if the body is clean |

**Built 2026-08-29, PR #116 open, not yet on `main`** — branch
`fix/rag-eval-acceptance-fixtures`, commit `7fe277c`. Merges clean against
`7399e8a`; `filename-pii` is not in `origin/main:rag_eval/hygiene.py` yet, so
until #116 merges a run still admits a borrower-shaped filename. Re-check with
`git show origin/main:rag_eval/hygiene.py | grep filename-pii` rather than
trusting this line. Measured before assuming, and the two items did not behave alike:

| item | before | after |
|------|--------|-------|
| `FIX-NEG-BORROWER-CONTENT` | already refused (`ssn`, `dob`, `bank` in body) | unchanged |
| `FIX-NEG-BORROWER-FILENAME` | **admitted — `passed=True`** | refused, `filename-pii` |

`scan_file` read the body only. The name does not stop at admission: `_slug` derives
every chunk id from it, so a borrower-shaped filename reaches the index, the report
and the retrieved chunk ids. It now scans `path.name` with the same detectors, before
reading the body, and tags the finding `filename-pii` rather than the underlying type
— redacting the body of a file whose *name* is the offender fixes nothing. The sample
is the suffix, never the name.

False positives were measured first, across all 18 files in her packet plus the repo's
own `policies/`: none trip. A name check that refuses a real corpus file would be
worse than no check.

Four tests; the two that matter were watched red first, and `make prove` reports
PROVEN. Note what they pin: synthetic files mirroring her fixture *names*, since her
fixtures live outside the repo and cannot be committed. Her actual two files were
verified by hand against the fix — both now refuse, with distinct reasons.

The 28 questions themselves are fine: `officer-questions-and-acceptance.jsonl` and
`gold_v2.json` match exactly, both directions, verified 2026-08-29.

### 2. Full 28-case rehearsal on TF-IDF

```bash
unset RAG_EMBEDDER
export RAG_JUDGE=...        # see the decision below
python3 -m rag_eval.run \
  --base <packet> --gold <gold set> \
  --manifest <packet>/SHA256SUMS.txt \
  --displayed-summaries-manifest <summaries dir>/SHA256SUMS.txt
```

Both manifests, always. An earlier version of this step passed only `--manifest`,
which left the summaries audit unexercised until the graded pass — a non-Titan
failure mode skipped by the step that exists to catch those.

**DECIDED 2026-08-29: both, in two phases, and neither uses one of her cases.**

*Where the judge lives is not a question.* LangSmith is not a model provider — no
inference happens there, so it was never an alternative to Bedrock, only a recording
layer on top. That layer is excluded twice: S-10 says prompts, passages and provider
responses persist nowhere, "logs and traces included", and her authorization covers
Bedrock under the existing AWS grant, not an outside vendor. It is enforced rather
than documented — `BedrockEvaluator.__init__` refuses to construct while
`LANGSMITH_TRACING` is set, checked before anything else, with a test.

#### Phase A — fake judge, all 28 rows. Free, no provider.

    cd <corpus dir>
    PYTHONPATH=<repo root> python3 rehearse_fake_judge.py

`rehearse_fake_judge.py` lives beside the gold set, deliberately outside the repo so
it can never be committed as production surface. It patches `make_evaluator`, runs
all 28, writes `rehearsal-report-<date>.md` with a NOT-A-DELIVERABLE banner, deletes
the copy `run()` leaves in the packet, and re-verifies 18 OK. It fails loudly unless
28 cases grade and 8 topics render.

The banner matters: `run.py` derives `judge_backend` from "an evaluator exists"
(`run.py:1420`, hardcoded `"bedrock"`), so a fake run's report names a backend that
never ran. True today because bedrock is the only backend; still false on this
report, hence the banner and the distinct filename.

**Ran 2026-08-29: PASS.** 28 graded, 8 topics, 18 OK.

One lesson from that run, worth not repeating: the fake first chose verdicts from a
period-4 counter, and every `records_retention` and `apr_finance_charge` row sits at
index 3 mod 4 — so all of them landed on `human_review` and two whole topics reported
`n/a (0 graded)`. A periodic fake manufactures the exact failure the rehearsal exists
to detect. It now selects by a hash of the query id.

#### Phase B — one real Bedrock call, on an INVENTED case.

    cd <corpus dir>
    PYTHONPATH=<repo root> AWS_REGION=us-east-1 python3 smoke_bedrock_judge.py

The obvious version — smoke one of her 28 — creates an S-7 problem for no gain:
reading that verdict and adjusting anything is prompt tuning against seen results.
So the case is synthetic (a widget warranty, nothing resembling her corpus or
consumer lending), which removes the question entirely.

It proves the credential resolves, model access exists in the region, the envelope
parses, the reply is the fixed four-key JSON, and the verdicts are in-vocabulary —
including the prohibited axis's inverted polarity. It refuses to run without an
explicit `AWS_REGION`, without a credential, or with `LANGSMITH_TRACING` set.

**Judge the contract, not the answer.** If the verdict looks wrong on a synthetic
case, that is not licence to edit the prompt.

Not yet run: no Bedrock credential is present in the environment. This is the only
step that spends money, and it is one call.

#### Why Phase B is not optional

Skip it and the first real Bedrock contact in this project is the pass that cannot be
repeated. The failure path is one retry, then a raised error — a partial run, mid-set,
with no second attempt. Every failure Phase B catches would otherwise surface there.

#### Before moving on

Verdicts on all three axes, three rates for all eight topics, and `human_review` not
owning a column.

### 3. The graded pass

```bash
export RAG_EMBEDDER=bedrock
export RAG_JUDGE=bedrock
export AWS_REGION=us-east-1
# credential in AWS_BEARER_TOKEN_BEDROCK — env var only, never a literal
```

Preconditions, each of which fails the run rather than degrading it:

- `LANGSMITH_TRACING` unset — `BedrockEvaluator.__init__` refuses to construct
  otherwise, and it checks before anything else.
- `LLM_TRACE_CONTENT` false.
- `AWS_REGION` explicit. Region discovery is excluded; Bedrock model access is
  granted per region.

Record from the run's own output: threshold, embedder signature, provider calls,
retries, input-token total, and the judge call count. The threshold **recalibrates** —
it is bound to one (corpus, embedder) pair and Titan moves the embedder side, so the
TF-IDF value `0.13666298135750454` does not carry over.

Approved model ids: generator `us.anthropic.claude-haiku-4-5-20251001-v1:0`,
embedding `amazon.titan-embed-text-v2:0` at dimension 1024, both `us-east-1`. The
`us.` prefix is a cross-region inference profile — `us-east-1` is accurate as the
*calling* region and would be overstated as a residency claim. Report the calling
region.

### 4. Clean up, every time

```bash
rm -rf <packet>/rag_eval
cd <packet> && shasum -a 256 -c SHA256SUMS.txt   # expect 18 OK
```

Never commit the packet or the summaries package. Public fork, and her approval covers
indexing, not publishing.

### 5. The client report

Retrieval plus the three verdict rates by topic, never pooled (S-4). State the S-6
interpretation rather than leaving it implicit — conclusion axis only, so the four
literal-backed rows still reach the model on summary and prohibited. Disclose the
Bedrock calling region. Empty the `human_review` bucket by hand; it counts in neither
direction (S-9), and its size says something about the evaluator as well as the corpus.

## A risk to state in the report, not to fix

**Four of the eight topics have exactly one case**: `debt_to_income`, `fee_schedule`,
`interest_rate`, `apr_finance_charge`. If the judge returns `human_review` for that
single case, the topic reports `n/a (0 graded)` — no rate at all — and S-9 is right to
count it neither way. S-4 keeps all eight topics in scope, and S-7 gives no second
pass, so a single uncertain verdict can erase a topic from the deliverable.

Nothing to build. Say it in the report rather than letting her find a blank row.

## Deferred, not blocking

- ADR 0019 vocabulary-gap amendment.
- ADR 0007 rule 6 — naming convention superseded by #96.

## Facts worth not re-deriving

- Gold set: 28 rows, 8 topics. Four carry a `support_literal`: q01 "30 days",
  q03 "12 CFR 1002.9", q04 "25 months", q24 "36 percent".
- `apr_finance_charge` has exactly one case, q24, and it is literal-backed. The
  whole-case reading of S-6 would blank that topic on all three axes, which is why
  conclusion-axis-only is the reading that survives S-4. Settled empirically, not by
  argument.
- The summaries join key is **`expected_conclusion_id`**, not `question_id`. The gold
  set's `displayed_summary_id` holds `Q01-ACCEPTABLE-CONCLUSION`; `question_id` holds
  `Q01`. Keying on the wrong one resolved nothing and raised nothing —
  `require_displayed_summaries` now makes any future drift loud.
- `RAG_JUDGE` defaults to `none`. No dry run and no CI gate can spend a provider call
  by accident.
- `rag-eval-import-gate` passes on the branch (real exit 0, all three stages). An
  earlier `DeadlineExceeded` was a transient local build timeout.
- A harness-reported "exit code 0" on a backgrounded command is the *pipeline's* status,
  not the script's. Three times this session it meant nothing. Capture `$?` from the
  script itself.
