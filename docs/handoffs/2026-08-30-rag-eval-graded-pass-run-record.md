# Run record — the graded Titan pass, 2026-08-30

> **Where the inputs live.** The policy packet, the displayed-summaries package,
> the gold set and the client-facing correspondence are NOT in this repository and
> must not be added to it: this is a public fork, and the client's approval covers
> indexing her material, not publishing it. Paths to them appear below as
> `<packet>`, `<summaries>` and `<corpus dir>`. This document records the method,
> the decisions and the measured results, which is what a future session needs.



This is the raw capture of the pass, written so a client report can be produced
later without re-deriving anything. It is not itself the report. Companion files:
`docs/handoffs/2026-08-29-rag-eval-graded-pass.md` (the sequencing and the why),
`docs/rag-eval-run-pipeline.md` (the pipeline), ``docs/runbook-rag-eval-graded-pass.md`` (the how).

The pass ran once. ADR 0022 S-7 allows one bounded pass, and every case is now
spent — nothing below can be re-measured.

## 1. Code the pass ran on

`origin/main` = `e5f6ea7` (merge of PR #119). Executed from a detached worktree at
that commit so the working checkout's unrelated uncommitted changes could not
affect it.

Three changes had to land first, in this order:

- **#116** `fix/rag-eval-acceptance-fixtures` (`64f4871`) — `scan_file` now scans
  `path.name` before reading the body and tags the finding `filename-pii`. Without
  it a borrower-shaped filename is admitted and reaches the index, the report and
  the retrieved chunk ids via `_slug`.
- **#117** `chore/address-pr-turn-budget` (`8c70f9a`) — unrelated, in the base.
- **#119** `fix/rag-eval-fenced-reply` (`e5f6ea7`) — `_first_json_object()`
  extracts the first balanced JSON object from the model's reply. **Found by the
  Phase B smoke, not by any test.** See §7.

## 2. Exact invocation

    cd <corpus dir>
    env -u LANGSMITH_TRACING \
      PYTHONPATH=<detached worktree at e5f6ea7> \
      RAG_EMBEDDER=bedrock RAG_JUDGE=bedrock AWS_REGION=us-east-1 \
      python3 -m rag_eval.run \
        --base <packet> \
        --gold gold_v2.json \
        --manifest <packet>/SHA256SUMS.txt \
        --displayed-summaries-manifest <summaries>/SHA256SUMS.txt

Credential in `AWS_BEARER_TOKEN_BEDROCK`, env var only, never a file.
`LANGSMITH_TRACING` unset (S-10; `BedrockEvaluator.__init__` refuses to construct
otherwise). Both manifests passed, so the summaries audit was exercised.

Console log: `graded-pass-20260830-111157.log`.
Report copied out of the packet before cleanup: `graded-pass-report-2026-08-30.md`.

## 3. Inputs

| input | value |
|---|---|
| packet | `<packet>`, 18 files, manifest-verified |
| gold set | `gold_v2.json` — 28 rows, 8 topics |
| displayed summaries | `<summaries>`, own manifest |
| documents admitted | 6 scanned, 0 refused |
| chunks indexed | 66 |

## 4. Providers, cost, provenance

| | |
|---|---|
| Embedding backend | `bedrock-v1:amazon.titan-embed-text-v2:0:1024` |
| Embedding calls | 94 (66 chunks + 28 queries) |
| Embedding retries | 0 |
| Embedding input tokens | 6,644 |
| Cache | disabled — the provider path is cacheless since #97 |
| Judge model | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| Judge calls | 28 |
| Calling region | `us-east-1` |

`us.` is a cross-region inference profile. `us-east-1` is accurate as the CALLING
region and would be overstated as a residency claim. Report the calling region.

## 5. Threshold

**0.3007877649147387**, recalibrated by this run.

Bound to exactly one pair: (corpus of 66 chunks from `<packet>`,
embedder `bedrock-v1:amazon.titan-embed-text-v2:0:1024`). It must be re-derived
when either side moves. The TF-IDF value `0.13666298135750454` does not carry over
and never did.

`POLICY_RETRIEVAL_MIN_SCORE` still has no committed default; unset remains the
operational kill switch (ADR 0019).

## 6. Results

### Retrieval

17 answerable — hit@1 0.71 · hit@3 0.88 · hit@5 0.94 · MRR 0.80.
6 unanswerable — **all 6 correctly below threshold**.
5 clarification cases — ambiguous by design, carry no frozen anchor, scored on
nothing and excluded from every rate including the abstention count.

At the calibrated threshold: **2 answerable would wrongly abstain, 0 unanswerable
retrieve with false confidence.**

### The Titan vs TF-IDF trade, measured on the identical corpus the same day

| | TF-IDF | Titan |
|---|---|---|
| threshold | 0.13666298135750454 | 0.3007877649147387 |
| answerable that would wrongly abstain | 0 | 2 |
| unanswerable retrieved with false confidence | 5 | **0** |

Titan trades two silent abstentions for eliminating all five false-confident
answers. For an officer-facing tool, answering confidently from a corpus that
cannot answer is the worse error. Same direction as the July 2026 result on the
9-chunk corpus, independently reproduced here at 66 chunks.

### Verdicts — three axes, never pooled (S-4)

| axis | graded | human_review | rate |
|---|---|---|---|
| Expected conclusion | 25 supported | 3 | 1.00 |
| Displayed summary | 23 supported | 5 | 1.00 |
| Prohibited conclusion | 23 avoided | 5 | 1.00 |

Not one `unsupported` and not one `asserted` in 71 graded verdicts.

### By topic — all eight render, none blank

| Topic | Cases | Conclusion | Summary | Prohibited |
|---|---|---|---|---|
| `adverse_action` | 8 | 1.00 (7 graded) | 1.00 (6) | 1.00 (6) |
| `credit_decisioning` | 5 | 1.00 (5) | 1.00 (5) | 1.00 (5) |
| `records_retention` | 4 | 1.00 (3) | 1.00 (2) | 1.00 (2) |
| `eligibility_rules` | 7 | 1.00 (6) | 1.00 (6) | 1.00 (6) |
| `debt_to_income` | 1 | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| `fee_schedule` | 1 | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| `interest_rate` | 1 | 1.00 (1) | 1.00 (1) | 1.00 (1) |
| `apr_finance_charge` | 1 | 1.00 (1) | 1.00 (1) | 1.00 (1) |

The single-case-topic risk named in the 2026-08-29 handoff did NOT fire: all four
one-case topics graded. It remains a real fragility for any future pass — one
`human_review` on a single-case topic erases that topic's row entirely.

## 7. THE FINDING THAT MATTERS MOST: human_review is a retention-cap artifact

**The 13 `human_review` verdicts are not the judge saying "uncertain".** They are
the harness refusing an over-long rationale.

`QueryEval.__post_init__` caps a rationale at `RATIONALE_MAX_CHARS = 240`
(`rag_eval/metrics.py:167`). When the model exceeds it, `run.py:1380` does not
truncate — truncating would still persist the first 240 characters of a retrieved
passage (S-10) — it downgrades every model-derived axis to `human_review` and
substitutes the fixed string `"evaluator rationale exceeded retention limit"`.

Five cases hit it: **`q01`, `q04`, `q13`, `q14`, `q16`.**

That fully explains the 3 / 5 / 5 split. `q01` and `q04` carry a `support_literal`
("30 days", "25 months"), so their conclusion axis was graded MECHANICALLY and
survived the downgrade; only their summary and prohibited axes fell. `q13`, `q14`
and `q16` have no literal, so all three axes fell — 3 conclusion, 5 summary,
5 prohibited. The arithmetic closes exactly.

Rationale lengths on the 23 cases that did grade: min 125, median 189, **max
exactly 240**. Three more — `q06`, `q07`, `q24` — landed within 20 characters of
the cap. The model writes to the boundary, so this is not a tail event: roughly a
third of the set is at or over the limit.

Consequences to state plainly in the client report:

- The empty `human_review` bucket says something about the CAP, not about the
  corpus and not about the evaluator's confidence. Presenting it as evaluator
  uncertainty would be wrong.
- The prompt asks for "under 200 characters" and the cap is 240. The model
  overruns both. Prompt and cap disagree with observed behaviour.
- S-7 gives no second pass, so these five cases cannot be recovered by re-running.

Not a defect to fix silently before the report — it changes what the numbers mean.

## 8. Caveats the report must carry

1. **No negative control.** Every graded verdict on every axis is positive. The
   pass contains no case with a deliberately wrong expected conclusion, so it does
   not demonstrate the judge CAN return `unsupported` or `asserted`. A reviewer
   will ask. S-7 means we cannot add one to this pass.
2. **The retention-cap artifact** — §7.
3. **Single-case topics** — four topics have one case each; one uncertain verdict
   erases a topic. Did not fire here.
4. **Calling region, not residency** — §4.
5. **S-6 is read as the conclusion axis only.** Settled empirically: the
   whole-case reading would blank `apr_finance_charge`, whose only case q24 is
   literal-backed, and S-4 keeps all eight topics in scope.
6. **The model does not return bare JSON.** It wraps its object in a markdown
   fence despite an explicit prompt instruction. Tolerated by the parser since
   #119; worth disclosing as an observed model behaviour.
7. **Rates are per target and never pooled** (S-4).

## 9. Chronology — what failed before the pass, and why the numbers are trustworthy

Kept because it is the evidence that the pass was not a first attempt that
happened to work.

1. **Fake-judge rehearsal (free), re-run post-#116** — 28 graded, 8 topics, 18 OK,
   hygiene gate 6 scanned / 0 refused. PASS.
2. **Credential rejected — malformed, not unauthorized.** `AccessDeniedException:
   Invalid API Key format: Must start with pre-defined prefix` on all three probes
   (Haiku via profile, Haiku bare id, Titan). Cause: the shell variable held the
   whole assignment line, `AWS_BEARER_TOKEN_BEDROCK=ABSK…`, so its length was
   157 = 25 + 132. Diagnosed by arithmetic before spending anything further.
3. **Phase B caught the fence.** First successful call returned the verdicts inside
   a ```json fence. `grade()` called bare `json.loads`, raised, and returned all
   three axes at `human_review`. On the pass that fires on all 28 cases — a report
   with no rates in it, unrecoverable under S-7. Fixed in #119 by parsing the first
   balanced JSON object rather than stripping the one wrapper observed; `make prove`
   PROVEN, red on the parent commit, green with the fix.
   Every prior test fed the evaluator bare JSON because that is what a fake
   returns. The shape exists only in a real reply.
4. **Phase B re-run: PASS.** No axis coerced, rationale 144 chars.
5. **Dry run on TF-IDF with the identical command line** — flags and paths proven,
   validation confirmed to run before any embedding call, packet restored 18 OK.
6. **The graded pass**, once.

## 10. Cleanup — done

    rm -rf <packet>/rag_eval
    ( cd <packet> && shasum -a 256 -c SHA256SUMS.txt )   # 18 OK

Verified: 18 OK, no `rag_eval` directory left in the packet. Neither the packet nor
the summaries package is ever committed — public fork, and her approval covers
indexing, not publishing.

## 11. Still open

- **The client report itself** — not written. Retrieval plus the three per-topic
  rates never pooled, S-6 stated as conclusion-axis-only, calling region disclosed,
  the `human_review` bucket enumerated with §7's explanation, the negative-control
  gap and the single-case-topic fragility both named.
- **Project memory** — `rag-eval-graded-pass-state.md` still says `origin/main` is
  `7399e8a` with #116 the only thing open. Now `e5f6ea7`, #116/#117/#119 merged,
  pass complete.
- **Deferred, unchanged:** ADR 0019 vocabulary-gap amendment; ADR 0007 rule 6.

## 12. Artifacts

| file | what |
|---|---|
| `graded-pass-report-2026-08-30.md` | the run's own report, copied out before cleanup |
| `graded-pass-20260830-111157.log` | console summary of the pass |
| `rehearsal-report-2026-08-29.md` | fake-judge rehearsal, NOT a deliverable |
| `smoke_bedrock_judge.py` | Phase B contract test, raw-payload assertions |
| `rehearse_fake_judge.py` | the free rehearsal |
