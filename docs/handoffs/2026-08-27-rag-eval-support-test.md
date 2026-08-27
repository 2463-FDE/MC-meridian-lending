# Handoff — the two-verdict support test and the graded Titan run (2026-08-27)

**Branch:** `feat/rag-eval-topic-axis` (PR #99) · **Base:** `origin/main` · **Repo:** `/Users/maha/Desktop/revature/MC-meridian-lending`
**Status:** The whole ingestion and scoring stack is merged. The client answered the blocking
question, widened the requirement, and authorized a narrow exception. **No Titan call has been
made.** Next build item is the two-verdict data model.

## Read the client record first

Every exchange with Dana is on two local-only branches. Read them without switching — the working
tree is routinely dirty and concurrent sessions hold it:

```bash
git show docs/client-asks-originals:docs/client-asks-2026-08-27-support-test-correction.md
git show docs/client-asks:docs/client-asks-2026-08-27-support-test-correction.md
git ls-tree -r --name-only docs/client-asks -- docs/ | grep client-asks   # list them all
```

- **`docs/client-asks-originals`** (`d7caa2b`) — verbatim, both directions. Never edit or summarise.
- **`docs/client-asks`** (`4c601f5`) — derived readings, the constraint registers, and the
  topic-mapping decision with its evidence.

Her 2026-08-27 reply is the governing document now. Its derived reading lists **S-1…S-10**
(settled), **C-1…C-7** (conflicts with what was built) and **O-6/O-7** (open). Start there rather
than re-deriving.

**Two gaps in the record, both known:** the 2026-08-25 outbound email was never captured at send
time, and the gated two-line reply naming the generator model id and AWS region exists only as a
description. She has those values; we did not record them.

## What's done

Merged to `main` on 2026-08-27, newest last. Cite these merge commits, not a tip sha —
a tip moves, a merge commit does not:

- `9aeb618` (#95) ranking-quality floor
- `e573401` (#96) manifest-digest corpus admission, compose env vars, `--base`/`--manifest`
- `eaeadad` (#97) the client's operating controls: no cache under a provider backend, no query or
  question text in reports, sanitized provider errors, two-attempt retry cap, refuse-if-tracing
- `916f9ee` (#98) `--gold` out-of-repo path, `outcome_class` + the four classes, per-class breakdown

**PR #99** `feat/rag-eval-topic-axis` — `topic` on a gold case validated against `TOPIC_CODES`, a
drift test against `POLICY_TOPICS`, `unmapped` deliberately outside the vocabulary, per-topic table
in the report. Its head is `25f69ed`; check whether it has merged before trusting anything below
that assumes it has not. 222 tests pass, smoke passes.

Her displayed-summaries package is verified: `SHA256SUMS.txt` 4/4 unedited, 28 rows, and every
column labelled "frozen, byte-equal to the source packet" **is** — 0 mismatches across 28 rows and
7 columns.

## What's left

Ordered. Steps 1–4 need nothing from the client.

1. ~~**#99 has a stale base.**~~ **Done 2026-08-27.** A parallel session merged `main` in
   (`396f45a`) and regenerated `docs/state.md` twice (`4a50246`, `e5c4f9e`); the `_metrics_section`
   and `_ALLOWED_GOLD_KEYS` conflicts with #98 are resolved. Two commits landed after that:
   `9ed8488` gives every committed gold case a topic and drops `unmapped` from the scored table,
   and `25f69ed` corrects the six/seven count in the `QueryEval.topic` comment. #99 reports
   `mergeable: clean` against `916f9ee`. Nothing to do here but merge it.
2. **The two-verdict data model — the largest remaining piece.** `metrics.py:34` on `main`
   (`:51` on #99, which added comment lines above it) has
   `correct: bool = field(init=False)`, one verdict. S-1 needs the expected conclusion and the
   displayed summary graded **separately and never merged**; S-9 needs a third state
   (`human_review`) that counts neither way. A boolean cannot carry it, and defaulting the third
   state to false would score uncertainty as failure — the specific thing she forbade.
3. **Freeze the displayed summaries.** Reuse `load_corpus_manifest` / `audit_corpus_against_manifest`
   (`rag_eval/run.py:46` and `:78`, on `main` since #96) against her `SHA256SUMS.txt`. Her ordering
   is explicit: frozen **before** retrieval runs, not alongside.
4. **Build gold set v2 itself** — the out-of-repo file `--gold` consumes: 28 rows, `topic` from the
   decision recorded on `docs/client-asks`, `expected` from the frozen anchors, both grading
   targets. All 17 anchors verified resolving via
   `Path(sourceDocument).stem.lower() + "#" + _slug(sourceHeading)`, 0 misses.
5. **Re-derive the threshold.** `POLICY_RETRIEVAL_MIN_SCORE=0.1609` was calibrated for a 9-chunk
   TF-IDF corpus and is meaningless at 66 chunks under Titan. Derive per (corpus version, embedder
   signature) and store with both.
6. **Query generation → human review of every string → freeze with a digest.** Approved as a
   one-time offline curation step. The `policies/fee_schedule.json` + agreement-test pattern is the
   house precedent.
7. **The evaluator.** S-7's three controls must be enforced in code, not intended: one bounded pass,
   one retry for technical failure only, no model fallback. The four mechanical cases must not
   consume a model call (S-6). It grades only — it cannot edit a conclusion, a summary or a decision
   (S-8). **It must live in `rag_eval/`, never on the product path** (C-6).
8. **The single Titan pass**, two arms, then her report: frozen set + digest, commit SHA, model ids
   and regions, call and retry counts, costs split by generator and Titan, literal vs generated,
   corpus and exclusion results, both support-test verdicts, logging confirmation.
9. **ADR 0019 amendment** — the vocabulary gap. **ADR 0007 rule 6** — still describes the naming
   convention as the corpus admission control, which #96 superseded.

## Blockers / open questions

- **None blocking.** She answered the support-test question and delegated the mapping outright.
- **O-6, ours:** whether to add a servicing/collections topic code. Deferred deliberately —
  changing the officer vocabulary inside the exercise that measures whether the vocabulary works
  conflates measuring with fixing. **The count is seven, not six:** Q24's frozen anchor is also in
  `SYN-POL-SERVICING-COLLECTIONS.md`, which her `needs_review` set missed. The code comment that
  said six is corrected on #99 (`25f69ed`); the count is not recorded anywhere else.
- **O-7, ours:** the four topic codes with no content in this corpus — retire, remap, or accept
  permanent abstention.
- **Her review burden is 17 rows, not 6.** The CSV carries a `review_suggested` tier her email did
  not mention. Report it as a property of the input; it is not a question for her.

## Key measurements (do not re-derive)

- Querying with each row's own `draft_topic`: the frozen anchor returns for **2 of 17**. Choosing
  whichever code retrieves best reaches **10 of 17**. The decision was to keep her labels — they are
  semantically sound, and remapping for score would tune the gold set against the system under test.
- Support test splits **4 mechanical / 13 interpretation-dependent**. The four are Q01 (30 days),
  Q03 (`12 CFR 1002.9`), Q04 (25 months), Q24 (36%).
- `claimIds` cannot anchor support: `policies/policy-manifest.csv` maps claims to *documents*, and
  gold `expected` (`document#heading`) is already stronger.
- All numbers above are TF-IDF and will move under Titan.

## Key files

- `rag_eval/metrics.py:34` on `main` (`:51` on #99) — the single `correct` bool step 2 replaces
- `rag_eval/run.py:210` `_ALLOWED_GOLD_KEYS`, `:227` `_OUTCOME_CLASSES` — widen for the new fields
- `rag_eval/run.py:46` / `:78` — manifest load and audit, reusable for the summaries
- `rag_eval/report.py:103` — now `{e.query_id}` only; #97 removed the question text. Keep it that way
- `services/origination-service/app/policy_retrieval.py:72` `POLICY_TOPICS` — the closed vocabulary
- `services/origination-service/app/policy_retrieval.py:108` `tool_result()` — returns
  `{status, score}`; the officer-path guarantee her exception does **not** touch

## How to verify / run

```bash
python3 -m pytest rag_eval/tests -q          # 222 on #99 at `25f69ed`
./scripts/smoke_rag_eval.sh                  # SMOKE PASS
cd services/origination-service && python3 -m pytest tests -q
./scripts/check_doc_paths.sh
```

Ingesting the packet — nothing moves into the repo:

```bash
cp -R <packet>/policies <workdir>/policies && cp <packet>/SHA256SUMS.txt <workdir>/
cd <workdir> && shasum -a 256 -c SHA256SUMS.txt
python3 -m rag_eval.run --base <workdir> --manifest <workdir>/SHA256SUMS.txt
```

**Both packets live in session scratchpads that will not survive.** The originals are in
`~/Downloads`, which the sandboxed shell cannot read — `ls` returns `Operation not permitted`, not
an empty directory. Copy them out with `!` first. Never commit either: public fork, and her approval
covers indexing, not publishing.

## Branch state

- `main` (`916f9ee`) = the client's real state, and it now holds the ingestion stack: manifest
  admission, the operating controls, the gold schema. Retrieval still runs the two original
  documents until a corpus is supplied at runtime. Cite `git show main:<file>` for client state.
- `feat/rag-eval-topic-axis` = PR #99, head `25f69ed`. Check its state before relying on it.
- `docs/client-asks` / `docs/client-asks-originals` = local-only, never pushed.

## Debt log refs

- **D16** (pgvector deferred) untouched — still 0 of 3 triggers at 66 chunks, one caller, no
  persistence requirement.
- ADR 0019 governs policy retrieval; ADR 0007 rule 6 needs the amendment in step 9.
- No new debt entries opened or closed.

## Next session: start here

#99 needs no rebase — its base is current and it is `mergeable: clean`. Merge it, then go
straight to step 2: replace the single `correct` bool in `rag_eval/metrics.py` with two
independent verdicts plus a `human_review` state, driven by tests written red first. Steps 1
and 9 of the list above are the only ones touched since this file was written.
