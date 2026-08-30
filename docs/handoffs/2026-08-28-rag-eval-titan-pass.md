# Handoff — the evaluator, then the graded Titan pass (2026-08-28)

> **Where the inputs live.** The policy packet, the displayed-summaries package,
> the gold set and the client-facing correspondence are NOT in this repository and
> must not be added to it: this is a public fork, and the client's approval covers
> indexing her material, not publishing it. Paths to them appear below as
> `<packet>`, `<summaries>` and `<corpus dir>`. This document records the method,
> the decisions and the measured results, which is what a future session needs.


**Branch:** none of your own — start from `origin/main` (`01f8cc1`) · **Repo:** `/Users/maha/Desktop/revature/MC-meridian-lending`
**Status (rebaselined 2026-08-29).** Steps 1 and 2 of this file's own list are now **done**
— #108 and #109 merged overnight, and the `support_literal` merge landed and is verified.
**No Titan call has been made.** Two build items remain: the `rationale` field, then the
evaluator. The pass now depends on the evaluator, because the
08-28 scope decision (retrieval **plus** support) means a run today would report the
support arm as `not_evaluated` and burn the single pass. Of the four things that gated the
pass, two closed on the client's approved model ids and two were decided 08-28; one minor
question is open (does the prohibited axis ride along).

Read `docs/handoffs/2026-08-27-rag-eval-support-test.md` for the client record and the
S-1…S-10 / C-1…C-7 registers; it is still the source of truth for the contract. This file
supersedes `docs/handoffs/2026-08-28-rag-eval-evaluator.md`, which is now wrong in two
places (see *Corrections* below).

Setup and pipeline detail live in `docs/rag-eval-run-pipeline.md` — read it before running
anything. **Both that file and this one were held untracked until the pass completed** —
the user's call on 2026-08-28, since the pre-run drafts carried packet paths verbatim into
a public fork. They are tracked from 2026-08-30: the pass is done, the paths are now
`<packet>` / `<summaries>` / `<corpus dir>` placeholders, and neither file quotes her
question text. The inputs themselves still never enter this repository — see the header.

## What's done

Merged, newest last. Cite the merge commit, never a tip sha:

- `1ff8cc1` (#101) report truth
- `b2d9892` (#102) gold anchors — a row names `source_document` + `source_heading`
- `26d00e8` (#103) assistant run telemetry
- `95d7858` (#104) threshold provenance — corpus signature bound to content, exhaustive
  cutoff sweep
- `be796f0` (#105) two support verdicts — `conclusion_verdict` / `summary_verdict`,
  four states, mechanical support check off `support_literal`
- `108100f` (#106) displayed summaries frozen **before** retrieval runs
- `8fa35fb` (#107) **the prohibited conclusion as a third grading target** — its own axis
  with its own states (`avoided`/`asserted`), `_UNECHOED_TEXT_KEYS` so her prose sits out
  the person-name guard, and a review-round fix (`27cb2be`) making the gold loader refuse
  a wrong-shaped set by naming the expected shape

- `4471bc4` (#108) `expected_conclusion` + `displayed_summary_id`, required together per row
  and all-or-nothing per file, plus a review-round fix carrying all three gold targets
  through to `QueryEval` (they were validated then dropped, so nothing downstream had a
  target). **Merged 2026-08-29 02:19 UTC — but into `feat/rag-eval-prohibited-conclusion`,
  not `main`**, because its base was never repointed.
- `01f8cc1` (#109) carried that branch up to `main`. This is what kept #108 from being
  stranded — the stacked-PR shape that stranded #57 and nearly stranded #45/#46. Check the
  base of a stacked PR *before* the parent merges, not after.

**0 PRs open.** `origin/main` is `01f8cc1`; the suite is **293 passing** there.

## What's left

Ordered. Steps 1 and 2 are done; 3 and 4 need nothing from the client.

1. ~~**Settle #108.**~~ **DONE 2026-08-29** by a parallel session, overnight — merged as
   #108 into `feat/rag-eval-prohibited-conclusion` (`4471bc4`), then lifted to `main` by
   #109 (`01f8cc1`). No base repoint was needed in the end; a second PR carried it up.
   Nothing to do here. The worktree at `.claude/worktrees/feat-rag-eval-conclusion-fields`
   is clean at `0501f55` and can be removed.
2. ~~**Merge `support_literal` into the gold set.**~~ **DONE 2026-08-29.** The four literals
   are in the gold set (held outside this repository): `q01` "30 days", `q03` "12 CFR 1002.9", `q04`
   "25 months", `q24` "36 percent". Verified by a TF-IDF run — the expected-conclusion arm
   now reports **4 supported of 4 graded**, the other 24 `not evaluated` pending the
   evaluator. Pre-merge backup kept as `gold_v2.json.bak-*`. Packet re-verified 18/18 after
   deleting the `rag_eval/` output the run writes into `--base`.
3. **Add the `rationale` field, carry the verdicts through, and add per-topic verdict
   rates.** S-8/S-10 want verdicts plus one line; `rationale` exists only in comments
   (`metrics.py:24`, `report.py:284`), not as a field on `QueryEval`. Same PR must add
   per-topic breakouts for all three verdict axes — S-4 requires by-topic and today only
   retrieval has it (`TopicStat` is `n`/`correct`; the verdict stats are pooled).
   ~200–300 lines, one PR.
4. **The evaluator.** S-7's three controls as code, not intent: one bounded pass, one retry
   for **technical** failure only, no model fallback. Grades only; cannot edit a conclusion,
   summary or decision (S-8). It is the only producer of a `human_review` verdict and the
   only thing that can move `prohibited_verdict` off `not_evaluated` — three report states
   wait on this one module. ~250–350 lines with tests, one PR; the bulk of the work.
   - S-6 is read **conclusion-axis-only** (see blockers): the four mechanical rows are
     graded by the evaluator on summary and prohibited. 80 gradings total.
   - Model: `us.anthropic.claude-haiku-4-5-20251001-v1:0`, `us-east-1`, `temperature: 0`
     (Haiku 4.5 still accepts sampling parameters). Auth: `AWS_BEARER_TOKEN_BEDROCK`, no
     code change — botocore resolves it and `BedrockEmbedder` passes no explicit credentials.
   - **C-6 needs a real assertion.** `rag_eval/` already ships inside the origination image
     (`COPY rag_eval ./rag_eval`, held by the blocking `rag-eval-import-gate`), so "it lives
     in `rag_eval/`" does not by itself keep the judge off the product path. Assert no
     `services/` import, no credential read at import time.
   - **LangSmith off for the judge, by decision.** `refuse_traced_provider_run`
     (`run.py:484`) reads `IS_PROVIDER_BACKED` off the *embedder* and is blind to a judge
     client; it needs its own refusal on the same variable.
5. **The single Titan pass**, two arms, then her report. Cannot be spent before step 4
   exists — see the blockers below.
6. **ADR 0019 amendment** — the vocabulary gap. **ADR 0007 rule 6** — still describes the
   naming convention as the corpus admission control, which #96 superseded.

## Blockers / open questions

- ~~**The grading-scope decision is unmade, and it is the user's.**~~ **DECIDED 2026-08-28:
  retrieval plus the support verdicts.** Not retrieval alone. S-7 makes it unrevisable.
  **This reorders the work: the Titan pass can no longer be spent before the evaluator
  exists** — `summary_verdict` has no mechanical producer, so a pass run today would report
  the support arm as `not_evaluated` and burn the one pass. It also promotes the
  `support_literal` merge from nicety to prerequisite. Report two rates, per topic, never
  merged (S-1, S-4).
- **DECIDED 2026-08-28: S-6 is read as the conclusion axis only**, so 80 gradings — 24
  conclusion, 28 summary, 28 prohibited. Decisive reason: `q24` is the only
  `apr_finance_charge` row *and* one of the four mechanical ones, so a whole-case reading
  leaves that topic at n=0 on the summary axis, and S-4 keeps all eight topics in scope.
  Also: whole-case would measure the two rates over different populations (28 vs 24), and
  `_support_verdict` (`run.py:1148`) already scopes the exclusion to the conclusion axis.
  **This is an interpretation of her instruction — state it in the report** in one line.
- Retrieval measurement, already made — do not re-derive: answerable top-1 scores span
  0.1464–0.3555, abstention spans 0.1269–0.3561, so the highest abstention case outranks
  every answerable one. An exhaustive cutoff sweep floors at 5 errors of 23, exactly what
  the calibrated 0.1367 already achieves. Abstention at 1 of 6 is a property of the corpus;
  no threshold fixes it.
- ~~**Still open: does the prohibited axis ride along?**~~ **DECIDED 2026-08-29: yes,
  graded.** Three graded axes, three rates, never merged. Cost measured: her prohibited text
  is 2,376 chars over 28 rows (~21 tokens per case, ~594 for the run) — under a thousand
  tokens on a ~40K run, fractions of a cent. **That holds only if the three axes share one
  prompt per case**; per-axis calls add 28 calls instead.
  - **Caveat — polarity inversion, and batching invites it.** Two axes where `supported` is
    good share a prompt with one where `avoided` is good. `metrics.py` already warns in
    prose that "the prohibited conclusion is supported" reads as the opposite of the
    finding, and S-7 allows no retry for a bad result. Mitigate in the prompt: name each
    axis by its own state words, and have the evaluator emit the enum strings rather than a
    boolean the caller maps.
  - **Caveat — per-topic verdict rates do not exist, and this decision needs them.** S-4
    requires by-topic, not pooled, but `TopicStat` carries only `n`/`correct` (retrieval)
    and all three verdict axes are pooled over the whole set. Not prohibited-specific — it
    applies to every verdict axis now inside the graded result. **Fold into step 3**, with
    the `rationale` field.
  - Clean, no action: all 28 rows carry a prohibited conclusion; `_UNECHOED_TEXT_KEYS`
    covers it and `report.py` reads no gold text (S-10 holds);
    `test_prohibited_conclusion.py:191` already pins `PROHIBITED_STATES[0] == AVOIDED`, so a
    reorder that would invert the rate fails the suite.
- ~~**Two client records disagree about the evaluator reading policy text.**~~ **CLOSED** —
  the client named a **Bedrock** generator model, so policy text goes to Bedrock under the
  existing approved AWS grant, not to an outside vendor. That settles the 2026-08-25 "Not
  approved" line against S-5's narrow exception. The report must still state the data path
  (Bedrock, region, both model ids) so her stop condition is visibly satisfied.
- ~~**The generator model id and AWS region are still unrecorded.**~~ **CLOSED** — approved
  values: generator `us.anthropic.claude-haiku-4-5-20251001-v1:0`, embedding
  `amazon.titan-embed-text-v2:0`, dimension 1024, region `us-east-1` for both. The
  embedding pair already matches the code (`run.py:464`, `embedder.py:125`) exactly.
- **New, minor: the `us.` prefix is a cross-region inference profile, not a region pin.**
  The call originates in `us-east-1`, but Bedrock may serve it from any US commercial region
  in the profile. Word the report as the *calling* region, or ask whether she wants the
  profile-free id. Titan's id carries no profile prefix, so that half is unambiguous.
- **The credential is a Bedrock-scoped API key, and it needs no code change.** Export
  `AWS_BEARER_TOKEN_BEDROCK`: botocore resolves a bearer token from
  `AWS_BEARER_TOKEN_<SIGNING_NAME>`, `bedrock-runtime` signs as `bedrock` and lists
  `smithy.api#httpBearerAuth`, and `BedrockEmbedder` passes no explicit credentials
  (`embedder.py:205`). One token covers both Titan and the judge — both are
  `bedrock-runtime`. A Bedrock-scoped key is *narrower* than the broad AWS credentials her
  approval already covers, so record it in the report but do not treat it as a blocker.
- **O-6/O-7 stay deferred, deliberately.** O-7 is three codes, not four: `debt_to_income`,
  `fee_schedule` and `interest_rate` carry only unanchored `no_match` questions — they are
  her abstention controls, not retirement candidates. `apr_finance_charge` has content (Q24).

## Corrections to the previous handoff

`docs/handoffs/2026-08-28-rag-eval-evaluator.md` is wrong on two points:

- It says gold v2 "lacks the `human_review` path". `human_review` has **no producer at all**
  and cannot have one until the evaluator exists — only a model can be uncertain (S-9). What
  gold v2 actually lacked was the `expected_conclusion` / `displayed_summary_id` pair, which
  is #108.
- It lists `fix/rag-eval-threshold-provenance` and `feat/rag-eval-two-verdicts` as
  unmerged. Both merged (#104, #105).

## Key files

Line numbers verified against `origin/main` (`01f8cc1`) on 2026-08-29. **They were wrong
in the 08-28 version of this file** — taken from an anticipated merge result rather than a
real tree, and off by up to 17 lines. Re-verify after any merge; they shift every time.

- `rag_eval/run.py:298` `_ALLOWED_GOLD_KEYS` — widen here for a new gold field
- `rag_eval/run.py:355` `_UNECHOED_TEXT_KEYS` — a field that is never embedded and never
  reported sits out the person-name guard; #108 adds `expected_conclusion` and
  `_SUPPORT_PAIR` beside it
- `rag_eval/run.py:434` `_NAME_ALLOWLIST` — phrase replacement, so it cannot cover a bare
  `Meridian` after a sentence-initial capital. Do not try to fix that with entries
- `rag_eval/run.py:467` `cache_enabled` — why the provider path is cacheless
- `rag_eval/run.py:484` `refuse_traced_provider_run` — a traced provider run is refused
- `rag_eval/run.py:502` `make_embedder` — `RAG_EMBEDDER`, and the blank-region refusal
- `rag_eval/run.py:605` `calibrate_threshold` — exhaustive; do not re-derive by hand
- `rag_eval/metrics.py:33` `UNSCORABLE_CLASS` — the `clarification` reported-not-scored precedent
- `rag_eval/metrics.py:63` `PROHIBITED_STATES` — `states[0]` is the numerator, which is the
  contract `_support_stat` reads; the order is load-bearing

## How to verify / run

```bash
python3 -m pytest rag_eval/tests -q     # 293 passed on origin/main (01f8cc1), 2026-08-29
./scripts/smoke_rag_eval.sh             # SMOKE PASS
./scripts/check_doc_paths.sh
./scripts/check_volatile_claims.sh
make prove REF=<sha> TEST=<path>        # REF matters: the tip is often a kb commit
```

Full 28-row TF-IDF run against her packet — **do this before spending the Titan pass**:

```bash
D=<corpus dir>
python3 -m rag_eval.run --base $D/<packet> \
  --gold $D/gold_v2.json \
  --manifest $D/<packet>/SHA256SUMS.txt
```

The run writes `rag_eval/eval_report.md` **and a cache into `--base`**. Delete
`$D/<packet>/rag_eval` after every run and re-check `shasum -a 256 -c
SHA256SUMS.txt` (expect 18 OK), or the packet stops matching `PACKAGE-INVENTORY.txt`.

Never commit the packet or the summaries package — public fork, and her approval covers
indexing, not publishing.

## Branch state

- `main` (`01f8cc1`) = the client's real state. Holds the ingestion stack, the anchors,
  `clarification` reported-not-scored, all three verdict axes, and the three gold targets
  carried through to `QueryEval`. Cite `git show main:<file>`.
- **0 PRs open.** `feat/rag-eval-conclusion-fields` (#108) and
  `feat/rag-eval-prohibited-conclusion` (#109) are both merged and both still exist on
  origin — delete them. The worktree at `.claude/worktrees/feat-rag-eval-conclusion-fields`
  is clean at `0501f55` and can go too.
- **A parallel session was active on this work on 2026-08-29** and merged both PRs
  overnight. Re-read `origin/main` before trusting any branch claim in this file, and check
  `git log -3` in a worktree before any amend or reset.
- `docs/client-asks-originals` was local-only and unpushed as of 2026-08-29.
  `docs/client-asks` has since been pushed. Read either with `git show <branch>:<file>`
  rather than switching.
- 11 stashes exist. Address a stash by message, never by index.

## Debt log refs

- **D16** (pgvector deferred) untouched — 0 of 3 triggers at 66 chunks. Persisting Titan
  vectors is that work, and it needs an ADR 0007 rule 6 amendment plus a PII re-review of a
  now-persistent store. Her authorization covers indexing, not retention.
- ADR 0019 governs policy retrieval; ADR 0007 rule 6 needs the amendment in step 5.
- No new debt entries opened or closed.

## Next session: start here

Steps 1 and 2 are done. **Start at step 3**: branch from `origin/main` (`01f8cc1`) and add
the `rationale` field, carrying the verdicts through `QueryEval` / `Aggregate` / `report`.
Then step 4, the evaluator, as a second PR.

Do not spend the Titan pass until the evaluator is merged — that ordering is a consequence
of the 08-28 scope decision, not a preference.

Housekeeping, cheap: delete the merged `feat/rag-eval-conclusion-fields` and
`feat/rag-eval-prohibited-conclusion` from origin, and remove the clean worktree at
`.claude/worktrees/feat-rag-eval-conclusion-fields`.

**A parallel session is working this same track** and merged both PRs overnight without
this file knowing. Re-read `origin/main` and the open-PR list before trusting any branch
claim here.
