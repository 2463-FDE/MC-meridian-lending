# Client asks — 2026-08-30 working log, graded pass results

**Audience:** Dana (VP Lending Ops, Meridian) · **Sent:** — · **Answered:** —

**Status: DRAFTED, NOT SENT.** The evaluation pass ran on 2026-08-30 and the results
are final. The email-fence copy is held outside this repository (it carries her packet
paths, her case ids and her corpus structure, and this is a public fork). The report
itself is in this repository and does not reproduce any of them:
`docs/reports/rag-eval-graded-pass-2026-08-30.md`, with the raw capture behind it at
`docs/handoffs/2026-08-30-rag-eval-graded-pass-run-record.md`. Five asks below come out
of the run, and every one of them is a decision only she can make.

## Run envelope — the fields she asked for after the run

Her 2026-08-26 reply set the post-run reporting terms: send the commit SHA, model and
region used, call and retry counts, cost, corpus and exclusion results, retrieval
results, and logging confirmation. Every field she named is below, so the pass is
auditable from this repository rather than only from the copy she is sent. Two of the
fields are reported with a stated limitation rather than as a clean measurement — cost
and the corpus count — and both limitations are named in their own rows and again under
"What the envelope does not establish" beneath the table. The envelope is not offered as
satisfying every field she named cleanly.

| Field she asked for | Value |
|---|---|
| Commit SHA | `e5f6ea7` — `origin/main`, the merge of PR #119. The pass ran from a detached worktree at that commit, so uncommitted work in the checkout could not affect it |
| Embedding model | `amazon.titan-embed-text-v2:0`, 1024 dimensions |
| Grading model | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
| Region | `us-east-1`, the region the calls were made to. The `us.` prefix is a cross-region inference profile, so this is not a data-residency claim and is not offered as one |
| Call counts | 94 embedding calls (66 passages + 28 questions), 6,644 input tokens; 28 grading calls, one per question |
| Retry counts | 0 embedding retries |
| Cost | Not billed-metered during the run, so this is a derived estimate, not an invoice figure. Titan embedding: 6,644 input tokens at the published on-demand rate of $0.02 per million input tokens for `amazon.titan-embed-text-v2:0` = **$0.00013**, about four orders of magnitude under her $1 Titan ceiling. The rate is the Amazon Titan Text Embeddings V2 on-demand row of the AWS Bedrock pricing page, `https://aws.amazon.com/bedrock/pricing/`, read 2026-08-30; no page snapshot or revision id was captured, so the arithmetic reproduces only for a reader who accepts that rate. Grading cost is not derivable: the run captured 28 grading calls but no judge token counts, and her ceiling covers Titan cost specifically. See the limitation note below |
| Corpus result | The packet supplied 2026-08-27, 18 files, verified against its own SHA-256 manifest before and after the run. **6 candidate files scanned, 0 refused** by the content and filename checks; 66 passages indexed. "6 scanned" is the hygiene gate's candidate-file count, not a count of policy documents — the harness counts every file it scans (`rag_eval/run.py`, "candidate files scanned"), while only gate-passed markdown reaches the chunker. The 66 passages match the five-document measurement recorded in `docs/handoffs/2026-08-27-fix-policy-corpus-admission.md`, so the sixth candidate contributed no passages. See the limitation note below |
| Exclusion result | Neither whole-document exclusion fixture was among the 6 candidate files scanned. Both are refused by the hygiene checks — one on content, one on filename (the no-match table in `docs/client-asks/2026-08-26-gate0-data-path.md`) — and the run reported **0 refused**, so neither can have been scanned, whichever root it sat under. Her scope note places both outside the approved policy directory, which matches the packet layout. Note that the candidate scan takes in two roots, `policies/` and `kb_dump/` (`rag_eval/run.py`), and only `policies/` is audited against her manifest — see the limitation note below |
| Retrieval result | 17 answerable questions: hit@1 0.71, hit@3 0.88, hit@5 0.94, MRR 0.80. All 6 unanswerable questions fell below the confidence threshold. Threshold `0.3007877649147387`, bound to this one (corpus, embedding model) pair |
| Logging confirmation | The report records that no prompt, passage or model response is retained, that the embedding cache is disabled on the provider path, and that external tracing was excluded and is enforced in code — the evaluator refuses to start if tracing is enabled, checked before any call is made |

### What the envelope does not establish

Two rows above are reported with a limitation rather than as a clean measurement.
Named here rather than left for the table to read as a clean result on every field.

- **The sixth candidate file is not identifiable from this repository.** Her 2026-08-26
  condition is that the corpus holds exactly the approved policy files and excludes our
  samples and both fixtures. What the retained artifacts prove is the *boundary*, not the
  list: `audit_corpus_against_manifest` audits in both directions and is scoped to the
  packet's `policies/` directory, so a file sitting in that directory and absent from her
  manifest aborts the run instead of being indexed, and a listed file whose digest has
  moved does the same. That audit covers `policies/`, and the candidate scan covers more
  than that: the run scans `policies/` **and** `kb_dump/` (`rag_eval/run.py`), and a
  `kb_dump/` file is outside her manifest's scope by design — the legacy dump is governed
  by its own pinned-digest exception, not by her policy manifest. So the boundary argument
  holds for a sixth candidate under `policies/`, where an unlisted file aborts the run, and
  not for one under `kb_dump/`, which is never manifest-checked. Nothing retained here says
  which root the sixth candidate sat under. What is missing is its name and its root: the
  harness prints a per-file hygiene table, but that output stayed inside the packet and is
  not in this repository, so the count travelled and the list did not. Fix on our side, not
  hers: have the run record carry the admitted document ids and the root each was admitted
  from.
- **Cost is derived, not metered.** The Titan figure above is computed from the retained
  token count at a rate cited by URL and date with no snapshot captured, not read from
  a bill, and the grading cost is not derivable at all because judge token counts were
  never captured. Her ceiling is stated
  against Titan cost, which is the half that is derivable.

Grading rates, the topic breakdown and the limitations of the pass are in the report
rather than duplicated here; ask 1 and ask 4 below turn on them.

## Context she needs to have the asks make sense

The pass ran once over the 28 officer questions. Under the terms already agreed, each
question gets one bounded pass, so nothing below can be re-measured without her saying
so — which is what makes asks 2 and 3 decisions rather than engineering choices.

Two results qualify the numbers, and both are disclosed in the report rather than left
for her to find:

- Thirteen verdicts read `human review`. That is our own 240-character limit on the
  grader's written explanation, not uncertainty about her corpus. Five questions
  (q01, q04, q13, q14, q16) lost their model-graded verdicts to it, because we discard
  rather than truncate — truncating would still retain 240 characters of her policy
  text on disk.
- Every graded verdict on every target came back positive, and the set contains no
  question with a deliberately wrong expected conclusion. So the pass does not
  evidence that the grader can return a negative verdict.

## Asks

| # | Ask | Dana's response |
|---|-----|-----------------|
| 1 | May we add negative controls — questions carrying a knowingly wrong expected conclusion — to the next gold set? Without them we cannot evidence that the grader discriminates, and a row of 1.00s has to be read with that caveat attached. | |
| 2 | The grader's written explanation is capped at 240 characters, because we retain no model text beyond one line. The model writes past it: five of 28 questions lost their verdicts that way, and the median explanation on the ones that survived is 189 characters against a maximum of exactly 240. Do you want the cap raised, or the grader asked for a shorter explanation? Either is cheap; the cap is a retention decision, so it is yours. | |
| 3 | Recovering those five questions means grading them a second time. One pass per question is your term, not our judgement — do you authorise a second pass limited to q01, q04, q13, q14 and q16? | |
| 4 | Four topics — debt to income, fee schedule, interest rate, APR and finance charge — carry exactly one question each. All four graded cleanly this time, but a single uncertain verdict would have left the topic with no rate at all rather than a low one. May we add questions to those four before the next pass? | |
| 5 | The retrieval threshold trades two errors against each other. At the value we derived, two answerable questions would be wrongly declined and none of the unanswerable ones are answered with false confidence; the keyword baseline reverses that exactly (0 and 5). We chose to eliminate the false-confident answers. Do you weigh those two errors the same way, or should we re-derive the threshold against a stated preference? | |

## Notes for whoever picks this up

- Ask 5 is hers to answer, and it is the one with a real engineering consequence: the
  threshold is a single number bound to one (corpus, embedding model) pair, and it must
  be re-derived whenever either side moves. It is not portable and should never be
  copied forward. It is awaiting her answer like the other four, not incidental to them.
- Asks 2 and 3 travel together. Raising the cap without a second pass changes nothing
  about this run's numbers; a second pass without raising the cap would lose the same
  five questions again.
- Nothing here should be answered by us on her behalf. Where a previous ask went
  unanswered we recorded the working assumption; these five are all live.
