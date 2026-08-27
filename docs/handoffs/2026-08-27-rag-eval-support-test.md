# Handoff — the two-verdict support test and the graded Titan run (2026-08-27)

**Branch:** none — `feat/rag-eval-topic-axis` (PR #99) merged as `2525f96`. Start the next piece
from a fresh branch off `origin/main`. · **Repo:** `/Users/maha/Desktop/revature/MC-meridian-lending`
**Status:** The whole ingestion and scoring stack is merged. The client answered the blocking
question, widened the requirement, and authorized a narrow exception. **No Titan call has been
made.** Next build item is the two-verdict data model.

## The governing constraints

Her 2026-08-27 reply is the governing document. **Its constraints are reproduced below rather than
cited**, because the branches holding it do not reach a clean checkout:

- **`docs/client-asks-originals`** (`d7caa2b`) — verbatim, both directions. **No remote at all.**
- **`docs/client-asks`** (`4c601f5`) — derived readings and the topic-mapping decision. A remote
  exists (`origin/docs/client-asks`, `3c8172f`) but is **behind**: the 2026-08-27 file is not on it.

Neither is pushed on purpose, and the fix is not to push them. This repo is a **public fork**, and
her approval covers indexing the packet, not publishing her correspondence. So the derived,
non-sensitive constraints are inlined here — enough to implement against without the private refs.
On the authoring machine, read the full record without switching branches (the tree is routinely
dirty and concurrent sessions hold it):

```bash
git show docs/client-asks-originals:docs/client-asks-2026-08-27-support-test-correction.md
git show docs/client-asks:docs/client-asks-2026-08-27-support-test-correction.md
git ls-tree -r --name-only docs/client-asks -- docs/ | grep client-asks   # list them all
```

### Settled — S-1…S-10

| # | Constraint | Consequence for the build |
|---|---|---|
| S-1 | The support test does **not** narrow. Expected conclusion and displayed summary are two frozen targets. | Two verdicts per case, kept distinct. A merged verdict is refused by name. |
| S-2 | 28 synthetic displayed summaries delivered with a SHA-256 manifest. | A second frozen artifact, verified the way the corpus is, **before** retrieval runs. |
| S-3 | The draft question-to-topic mapping is explicitly ours to change. | Six rows (Q07, Q12, Q14, Q25, Q27, Q28) marked `needs_review` — servicing/collections questions with no code in the vocabulary. |
| S-4 | All eight officer topics stay in scope; report **by topic**, not pooled. | A reporting axis the harness did not have (shipped on #99). |
| S-5 | Option 2 authorized as a **narrow exception for this exercise**, not a rule change. | An offline evaluator may read policy text. The officer-facing path is untouched and must be reported as untouched. |
| S-6 | The four mechanical cases must **not** consume a model call. | Exclusion by instruction, not by preference. |
| S-7 | One bounded evaluator pass. One retry, technical failure only. No prompt tuning against seen results. No model fallback. | Three separate controls, each needing enforcement in code. |
| S-8 | The evaluator grades and nothing else. | Its outputs are verdicts plus one rationale line — never edits to a conclusion, a summary or a decision. |
| S-9 | Unsupported or uncertain verdicts go to human review, counting neither way. | A third verdict state. A boolean cannot carry it. |
| S-10 | Retention allowlist: case id, topic, source-section reference, the two verdicts, one human-reviewed rationale line. | Prompts, passages and model responses persist nowhere — logs and traces included. |

Her stop condition is explicit: **if the implemented data path differs from her description, stop
and ask again.**

### Conflicts with what was built — C-1…C-7

- **C-1** the gold loader refuses her data — `_ALLOWED_GOLD_KEYS` is a closed allowlist.
- **C-2** two verdicts is a data-model change — `QueryEval.correct` is one bool, and `Aggregate`
  counts against it.
- **C-3** per-topic reporting did not exist (closed by #99).
- **C-4** six questions have no topic code, and the naive fix lands on the product path.
- **C-5** retention is satisfied only once the operating controls merge (closed by #97).
- **C-6** the evaluator must live in `rag_eval/`, **never** on the product path.
- **C-7** S-7's three controls need code — an intention that is not enforced is not a control.

### Open — ours, not hers

- **O-6** the six `needs_review` rows: their assignment is settled — they take `unmapped`, by the
  rule under *What's left* step 4. What stays open is the vocabulary itself: whether to add a
  servicing/collections code and amend ADR 0019 and the gate.
- **O-7** whether the four codes with no content in this corpus are retired, remapped, or left
  permanently abstaining. S-4 keeps all eight in scope, which settles reporting, not the vocabulary.

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
- `2525f96` (#99) `topic` on a gold case validated against `TOPIC_CODES`, a drift test against
  `POLICY_TOPICS`, `unmapped` deliberately outside the vocabulary, per-topic table in the report

`origin/main` is `2525f96`; 223 tests pass there, smoke passes. A **local** `main` may be stale —
`git fetch` and read `origin/main`, never the local ref.

Her displayed-summaries package is verified: `SHA256SUMS.txt` 4/4 unedited, 28 rows, and every
column labelled "frozen, byte-equal to the source packet" **is** — 0 mismatches across 28 rows and
7 columns.

## What's left

Ordered. Steps 1–4 need nothing from the client.

1. ~~**#99.**~~ **Closed 2026-08-27 — merged as `2525f96`. No action.** History, for anyone
   reading a stale copy of this file: a parallel session merged `main` in (`396f45a`) and
   regenerated `docs/state.md` twice (`4a50246`, `e5c4f9e`), resolving the `_metrics_section` and
   `_ALLOWED_GOLD_KEYS` conflicts with #98; then `9ed8488` gave every committed gold case a topic,
   `25f69ed` corrected the six/seven count in the `QueryEval.topic` comment, and `c751a1f` pinned
   the all-`unmapped` report shape. Verify with
   `git merge-base --is-ancestor c751a1f origin/main && echo MERGED`.
2. **The two-verdict data model — the largest remaining piece.** `rag_eval/metrics.py:51` on
   `origin/main` has `correct: bool = field(init=False)`, one verdict. S-1 needs the expected
   conclusion and the displayed summary graded **separately and never merged**; S-9 needs a third
   state that counts neither way. A boolean cannot carry it, and defaulting the third state to
   false would score uncertainty as failure — the specific thing she forbade.

   The contract, so two sessions build the same thing:

   - **Verdict state** — one enum, three members, used for both targets:
     `supported` / `unsupported` / `human_review`. No boolean anywhere in the pair. No fourth
     member, and no `None` standing in for `human_review`.
   - **Per-case fields on `QueryEval`** — `conclusion_verdict` and `summary_verdict`, both of that
     enum, plus one `rationale: str` (S-8/S-10: one human-reviewed line, no passage text). The
     existing `correct` bool stays and keeps meaning **retrieval** correctness — it is a different
     axis from the support test and must not be folded into either verdict.
   - **Denominator rule** — each target aggregates independently. For each of the two:
     `n_graded = supported + unsupported`, `rate = supported / n_graded`, and `human_review` is
     reported as its own count, **never** in the numerator or the denominator. `n_graded == 0`
     reports `n/a`, not `0.0`. No combined "both supported" score is computed or printed —
     S-1 refuses a merged verdict by name.
   - **Gold JSON fields** (widen `_ALLOWED_GOLD_KEYS`, `rag_eval/run.py:229`) —
     `expected_conclusion: str` and `displayed_summary_id: str`, both required on a case whose
     `outcome_class` is not `no_match`; both refused on a `no_match` case. Unknown keys keep
     failing closed, as they do today.
   - **Report columns** (`rag_eval/report.py`) — the per-case table gains `conclusion` and
     `summary`, each rendering the enum value; the aggregate block gains one row per target with
     `supported / n_graded` and a separate `human_review` count. Keep `{e.query_id}` only —
     #97 removed the question text and it stays out (S-10).
   - **Acceptance** — a red-first test per bullet: a merged-verdict helper does not exist, a
     `human_review` case moves neither rate, `n_graded == 0` renders `n/a`, a gold case missing
     `displayed_summary_id` is refused, and a `no_match` case carrying one is refused.
3. **Freeze the displayed summaries.** Reuse `load_corpus_manifest` / `audit_corpus_against_manifest`
   (`rag_eval/run.py:46` and `:78`, on `origin/main` since #96) against her `SHA256SUMS.txt`. Her ordering
   is explicit: frozen **before** retrieval runs, not alongside.
4. **Build gold set v2 itself** — the out-of-repo file `--gold` consumes: 28 rows, `expected` from
   the frozen anchors, both grading targets. All 17 anchors verified resolving via
   `Path(sourceDocument).stem.lower() + "#" + _slug(sourceHeading)`, 0 misses.

   **`topic` is derived, not looked up.** The rule is complete over the delivered summaries packet
   (`displayed-summaries.csv`, columns `question_id`, `draft_topic`, `topic_review_status`):

   > `topic = draft_topic`, except a row whose `topic_review_status` is `needs_review`, which takes
   > the literal `unmapped`.

   That is the decision in full: **22 rows keep her label, 6 become `unmapped`, no ninth code.**
   The six are Q07, Q12, Q14, Q25, Q27, Q28 — the servicing/collections questions the eight-code
   vocabulary cannot express (S-3). `draft_topic` is populated on all 28 rows and every value is
   already inside `rag_eval/run.py::TOPIC_CODES`, so no row needs a judgement call. Why her labels
   stay rather than being remapped for score is under *Key measurements*; the full rationale is on
   `docs/client-asks` (`4c601f5`), authoring-machine only — the rule above does not need it.
5. **Re-derive the threshold.** `POLICY_RETRIEVAL_MIN_SCORE=0.1609` was calibrated for a 9-chunk
   TF-IDF corpus and is meaningless at 66 chunks under Titan. Derive per (corpus version, embedder
   signature) and store with both.

   The contract, because a threshold tuned on the graded set invalidates the run it grades:

   - **Calibration set — disjoint from the eval set, by construction.** Do not sample from gold set
     v2's 28 rows. Build a separate held-out file of officer-shaped queries plus the known-absent
     probes (the four topic codes with no content, O-7), and record its own digest. A row may
     appear in the calibration set or in gold set v2, never both; assert the id sets are disjoint
     in a test.
   - **Method** — sweep the candidate threshold over the calibration set only, and pick the value
     that satisfies the abstention criterion below. One sweep, recorded. No re-tuning after seeing
     a gold-set number — S-7's "no prompt tuning against seen results" applies to this knob too.
   - **Acceptance criterion** — every known-absent probe abstains (no false answer), and the
     retrieval floor from #95 (`9aeb618`) still holds on the calibration set. If both cannot be
     met, report the conflict rather than picking a midpoint.
   - **Output** — write the chosen value with `(corpus_digest, embedder_signature)` alongside it,
     to the run report and to `.env.example` as a documented example only. The service still reads
     `POLICY_RETRIEVAL_MIN_SCORE` from the environment with **no committed default**: unset stays
     the operational kill switch (ADR 0019).
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
  The six `needs_review` stand-ins take `unmapped` instead: a wrong label that scores is worse than
  no label, and `unmapped` records that the vocabulary cannot express those questions at all.
- Support test splits **4 mechanical / 13 interpretation-dependent**. The four are Q01 (30 days),
  Q03 (`12 CFR 1002.9`), Q04 (25 months), Q24 (36%).
- `claimIds` cannot anchor support: `policies/policy-manifest.csv` maps claims to *documents*, and
  gold `expected` (`document#heading`) is already stronger.
- All numbers above are TF-IDF and will move under Titan.

## Key files

All line numbers are against `origin/main` at `2525f96`.

- `rag_eval/metrics.py:51` — the single `correct` bool; step 2 keeps it for retrieval and adds the
  two support verdicts beside it
- `rag_eval/run.py:229` `_ALLOWED_GOLD_KEYS`, `:247` `_OUTCOME_CLASSES` — widen for the new fields
- `rag_eval/run.py:46` / `:78` — manifest load and audit, reusable for the summaries
- `rag_eval/report.py:126` — now `{e.query_id}` only; #97 removed the question text. Keep it that way
- `services/origination-service/app/policy_retrieval.py:72` `POLICY_TOPICS` — the closed vocabulary
- `services/origination-service/app/policy_retrieval.py:108` `tool_result()` — returns
  `{status, score}`; the officer-path guarantee her exception does **not** touch

## How to verify / run

```bash
python3 -m pytest rag_eval/tests -q          # 223 on `origin/main` at `2525f96`
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

**The durable copies are the two unpacked directories in `~/Downloads`, each beside its `.zip`.**
Session scratchpad copies exist and will not survive; treat them as caches, not as the record.
(An earlier note here said the sandboxed shell cannot read `~/Downloads` — that is no longer true;
it reads and verifies fine. If a future sandbox does refuse, copy out with `!` first.)

| Packet | Path under `~/Downloads/` | Files | `SHA256SUMS.txt` rows | `shasum -a 256 SHA256SUMS.txt` |
|---|---|---|---|---|
| Policy corpus + officer questions | `Mahalakshmi-Meridian-Policy-Client-Inputs-Only-2026-08-25/` | 19 | 18 | `c6037c2c92c2a1412d9f266d1624cc8a07acf5c1ce046667c622d50958397a9d` |
| Displayed summaries | `Mahalakshmi-Synthetic-Displayed-Summaries-2026-08-27/` | 5 | 4 | `9d641ce9aadfb6d5025a1ccf0ce1e95e98b5675f318fc3ed7feebdefd27e3447` |

The corpus packet holds `policies/` (5 `SYN-POL-*.md` + `policy-manifest.csv`),
`officer-questions/`, `acceptance/`, `client-decisions/`, `scope/`, `sources/`,
`CORPUS-SHA256SUMS.txt` and `PACKAGE-INVENTORY.txt`. The summaries packet holds
`displayed-summaries.csv`, `DISPLAYED-SUMMARIES.md`, `TOPIC-MAPPING-REVIEW.md` and `README.md`.

Confirm both before use — this checks the manifest itself, then its contents:

```bash
cd ~/Downloads/<packet> && shasum -a 256 SHA256SUMS.txt && shasum -a 256 -c SHA256SUMS.txt
```

Never commit either: public fork, and her approval covers indexing, not publishing.

## Branch state

- `origin/main` at the time of writing = `2525f96`, #99's merge commit. It is the client's real
  state, and it now holds the ingestion stack:
  manifest admission, the operating controls, the gold schema, the topic axis. Retrieval still runs
  the two original documents until a corpus is supplied at runtime. Cite `git show origin/main:<file>`
  for client state — a local `main` here is routinely behind.
- `feat/rag-eval-topic-axis` = PR #99, merged as `2525f96`. Nothing left on it.
- `docs/client-asks` = pushed but stale (`origin/docs/client-asks` is `3c8172f`, local is `4c601f5`);
  the 2026-08-27 file is local-only. `docs/client-asks-originals` (`d7caa2b`) has no remote at all.
  Both stay unpushed deliberately — see *The governing constraints*.

## Debt log refs

- **D16** (pgvector deferred) untouched — still 0 of 3 triggers at 66 chunks, one caller, no
  persistence requirement.
- ADR 0019 governs policy retrieval; ADR 0007 rule 6 needs the amendment in step 9.
- No new debt entries opened or closed.

## Next session: start here

**#99 is merged (`2525f96`). Step 1 is closed; do not re-open it.** Confirm with
`git fetch && git merge-base --is-ancestor c751a1f origin/main && echo MERGED`, then branch off
`origin/main` and start step 2: keep `correct` in `rag_eval/metrics.py:51` as the retrieval axis and
add `conclusion_verdict` / `summary_verdict` beside it over the three-member enum, to the contract
written under step 2, driven by tests written red first.

If that confirmation does not print `MERGED`, the branch has diverged since this file was written —
re-check conflicts and the 223-test count before doing anything else.
