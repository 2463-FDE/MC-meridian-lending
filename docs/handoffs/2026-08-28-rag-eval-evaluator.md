# Handoff — the evaluator and the graded Titan run (2026-08-28)

> **Where the inputs live.** The policy packet, the displayed-summaries package,
> the gold set and the client-facing correspondence are NOT in this repository and
> must not be added to it: this is a public fork, and the client's approval covers
> indexing her material, not publishing it. Paths to them appear below as
> `<packet>`, `<summaries>` and `<corpus dir>`. This document records the method,
> the decisions and the measured results, which is what a future session needs.


> **SUPERSEDED 2026-08-29 — do not act on this file.**
> Read `docs/handoffs/2026-08-28-rag-eval-titan-pass.md` instead. Kept only for the three
> things it records that nothing else does: the person-name guard measurements (6/1/0 trips
> and the `If Meridian` phrase-replacement defect), the two-manifest scoping trap
> (`SHA256SUMS.txt` package-relative vs `CORPUS-SHA256SUMS.txt` corpus-relative), and the
> client's acceptance set being **30 items, not 28** (28 questions + 2 whole-document
> exclusion checks in `acceptance/`, both passing, neither recorded in the repo).
>
> Everything else here has decayed. Its `main` (`26d00e8`) is now 6 merges stale; both
> branches it calls "proposed, not merged" merged as #104 and #105; its steps 1, 2 and 4 are
> all built; its `run.py` line numbers are all wrong; its two open blockers are both settled.

**Branch:** none of your own — start from `origin/main` · **Repo:** `/Users/maha/Desktop/revature/MC-meridian-lending`
**Status:** Steps 1, 2 and 5 are done. The client's 28-question set now loads and runs
end to end. **No Titan call has been made.** Next build item is step 3, then the evaluator.

Read `docs/handoffs/2026-08-27-rag-eval-support-test.md` first — it holds the client record,
the two record gaps, and the constraint registers. This file supersedes only its "What's left".

## What's done

Merged, newest last. Cite these merge commits, not a tip sha:

- `1ff8cc1` (#101) report truth — corpus size interpolated, the denial-#6012 boilerplate no
  longer asserted over every row, `Military Lending Act` + `Servicemembers Civil Relief Act`
  allowlisted so her Q24 can load
- `b2d9892` (#102) gold anchors — a row names `source_document` + `source_heading` and the
  loader derives the chunk id under the active admission mode; `clarification` is reported,
  not scored

Proposed, not merged. Check each before relying on it:

- `fix/rag-eval-threshold-provenance` (head `3da0871`) — threshold tied to a content-bound
  corpus signature + embedder signature, exhaustive cutoff search, irreducible error count
- `feat/rag-eval-two-verdicts` (head `81207cb`) — `conclusion_verdict` / `summary_verdict`,
  four states, mechanical support check. **Neither `support_literal` nor `conclusion_verdict`
  is on `main`** — verify before writing code against them.

**Her corpus can only be admitted under a manifest.** All five policy filenames are non-slug,
so `unsafe_corpus_path_reason` refuses them under the naming convention. Every run needs
`--manifest`. This also means literal `expected` ids would have to be `doc-<12 hex>#heading`,
which is why anchors exist.

## What's left

Ordered. Steps 1–3 need nothing from the client.

1. **Freeze the displayed summaries.** Reuse `load_corpus_manifest` (`rag_eval/run.py:52`) and
   `audit_corpus_against_manifest` (`:84`) against her `SHA256SUMS.txt`. Her ordering is
   explicit: frozen **before** retrieval runs, not alongside. Package verifies 4/4 today.
2. **Finish gold set v2** as the out-of-repo file `--gold` consumes. A 28-row anchored version
   already runs (see *How to verify*); what it still lacks is the `human_review` path and the
   third grading target below.
3. **The evaluator** (S-7's three controls in code, not intent): one bounded pass, one retry
   for technical failure only, no model fallback. The four mechanical cases must not consume a
   model call — that exclusion is already built and is what `support_literal` does. It grades
   only; it cannot edit a conclusion, summary or decision (S-8). **It must live in `rag_eval/`,
   never on the product path** (C-6).
4. **Add `prohibitedUnsupportedConclusion` as a third grading target.** The authoritative JSONL
   carries it and the plan never counted it — an explicit negative naming what the evaluator
   must not conclude (Q13: "Answering with an invented cutoff or a single catch-all denial
   script"). The CSV drops it; read the JSONL.
5. **The single Titan pass**, two arms, then her report.
6. **ADR 0019 amendment** — the vocabulary gap. **ADR 0007 rule 6** — still describes the
   naming convention as the corpus admission control, which #96 superseded.

Step 6 of the old list (query generation) still looks **moot** — she supplied 28 questions.
Confirm before building anything.

## Blockers / open questions

- **The generator model id and AWS region are still unrecorded.** Step 5's report must state
  "model ids and regions". Titan's is pinned in code; the *generator's* exists only as a
  description. This is the one thing that needs her, and it blocks the report, not the run.
- **Abstention is 1 of 6, and no threshold can fix it.** Answerable top-1 scores span
  0.1464–0.3555; abstention spans 0.1269–0.3561, so the highest abstention case outranks every
  answerable one. An exhaustive cutoff search puts the floor at 5 errors of 23 — exactly what
  the calibrated 0.1367 already achieves. S-7 allows one bounded pass with no retry for a bad
  result. Decide before spending it whether the run is graded on retrieval alone.
- **O-6/O-7 stay deferred, deliberately.** O-7 is three codes, not four: `debt_to_income`,
  `fee_schedule`, `interest_rate` carry only unanchored `no_match` questions — they are her
  abstention controls, not retirement candidates. `apr_finance_charge` does have content (Q24).

## Key files

- `rag_eval/run.py:178` `corpus_doc_id` — why anchors exist; two disjoint id spaces
- `rag_eval/run.py:235` `_ALLOWED_GOLD_KEYS` — widen here for new gold fields
- `rag_eval/run.py:329` `_NAME_ALLOWLIST` — see the guard note below
- `rag_eval/run.py:460` `calibrate_threshold` — exhaustive; do not re-derive by hand
- `rag_eval/metrics.py:33` `UNSCORABLE_CLASS` — the `clarification` treatment, and the
  precedent for any future "reported, not scored" state
- `services/origination-service/app/policy_retrieval.py:72` `POLICY_TOPICS` — the closed
  vocabulary; `:108` `tool_result()` — the officer-path guarantee her exception does not touch

**Guard warning for step 3.** Adding her conclusion/summary text to the gold schema puts it
through the person-name guard. Measured on her 28 rows: `expected_conclusion_text` trips **6**
(Q08, Q10, Q12, Q15, Q18, Q24), `prohibitedUnsupportedConclusion` trips **1** (Q14),
`synthetic_displayed_summary` trips **0**. Zero carry actual PII. The phrases are
`Credit Manager`, `Credit Policy`, `Credit Policy Schedule`, `If Meridian`. The last one is a
*different defect*: the allowlist is phrase-replacement, so `Meridian Lending` does not cover
`If Meridian …`. Adding entries will not close it. Also note S-10 forbids that text reaching
the report at all — counts only.

## How to verify / run

```bash
python3 -m pytest rag_eval/tests -q          # 261 on the threshold branch
./scripts/smoke_rag_eval.sh                  # SMOKE PASS
./scripts/check_doc_paths.sh
make prove REF=<sha>                         # REF= matters: the branch tip is often a kb commit
```

Full 28-row run against her packet:

```bash
D=<corpus dir>
python3 -m rag_eval.run --base $D/<packet> \
  --gold $D/gold_v2_anchored.json \
  --manifest $D/<packet>/SHA256SUMS.txt
```

Current result (TF-IDF, 66 chunks, manifest admission): `answer` 11/12,
`manager_escalation` 4/5, `no_match` 1/6, `clarification` 5 reported outside every rate.
Every number here is TF-IDF and will move under Titan.

**The packet now has a durable home outside the repo** — the previous handoff said both
copies would not survive, and they nearly didn't:

```
<corpus dir>/<packet>/     # 18/18 digests OK
<corpus dir>/<summaries>/  # 4/4 OK
```

Never commit either — public fork, and her approval covers indexing, not publishing. `~/Downloads`
still returns `Operation not permitted`, not an empty directory. The run writes
`rag_eval/eval_report.md` and a cache **into `--base`**, so delete them from the packet after a
run or its file count stops matching `PACKAGE-INVENTORY.txt`.

Two manifests ship, scoped differently: `SHA256SUMS.txt` is package-relative
(`policies/X.md`, use with `--base <packet>`); `CORPUS-SHA256SUMS.txt` is corpus-relative
(use with `--base <packet>/policies`). Picking the wrong one reports "manifest declares no
approved files", which reads like a contaminated corpus. `CORPUS-SHA256SUMS.txt` is itself
unhashed by `SHA256SUMS.txt` and absent from the inventory's 19 — latent, they agree today.

The client's own acceptance set is **30 items, not 28**: 28 questions plus 2 whole-document
exclusion checks in `acceptance/`. Both exclusion checks pass today (one by body scan, one by
filename scan) and nothing in the repo records that they were ever run.

## Branch state

- `main` (`26d00e8`) = the client's real state. Holds the ingestion stack, the anchors, and
  `clarification` reported-not-scored. Cite `git show main:<file>`.
- The two proposed branches above are unmerged. Confirm before relying on either.
- `docs/client-asks` / `docs/client-asks-originals` = local-only, never pushed. Read with
  `git show docs/client-asks:<file>` rather than switching — the tree is often held by a
  parallel session.

## Debt log refs

- **D16** (pgvector deferred) untouched — still 0 of 3 triggers at 66 chunks.
- ADR 0019 governs policy retrieval; ADR 0007 rule 6 needs the amendment in step 6 above.
- No new debt entries opened or closed.

## Next session: start here

Freeze the displayed summaries (step 1) — reuse `load_corpus_manifest` / `audit_corpus_against_manifest`
at `rag_eval/run.py:52` and `:84` against her `SHA256SUMS.txt`, frozen before retrieval runs.
Confirm first whether the two proposed branches above have merged, since both touch the files
you will edit.
