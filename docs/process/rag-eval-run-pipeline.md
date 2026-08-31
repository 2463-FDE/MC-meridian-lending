# The rag_eval run: pipeline, and the prerequisites for the Titan pass

> **Where the inputs live.** The policy packet, the displayed-summaries package,
> the gold set and the client-facing correspondence are NOT in this repository and
> must not be added to it: this is a public fork, and the client's approval covers
> indexing her material, not publishing it. Paths to them appear below as
> `<packet>`, `<summaries>` and `<corpus dir>`. This document records the method,
> the decisions and the measured results, which is what a future session needs.


What `python3 -m rag_eval.run` actually does, stage by stage, and what has to be true
before the single graded Titan pass is spent.

The pass is bounded: **one run, plus one retry for technical failure only** (S-7). A bad
*result* is not grounds for a retry, and the provider path keeps no cache, so a second
pass re-embeds every chunk and every query at full cost. Everything below exists so the
first pass is the only one needed.

---

## 1. Readiness: what is and is not set up

### Ready

| Piece | State |
|---|---|
| Corpus admission by manifest | Built. Her five policy filenames are non-slug, so this is the only mode that can index her corpus. |
| Displayed-summaries freeze | Built, and runs before retrieval, which is the ordering she specified. |
| Hygiene gate | Built, blocking in CI. |
| Chunking + anchors | Built. 66 chunks from her five policy files. |
| Gold loader | Built: shape check, camelCase aliases, closed key allowlist, per-row and per-file rules, PII guards. |
| Bedrock/Titan embedder | Built and smoke-tested against a real key. Explicit-region refusal in place. |
| Retrieval, threshold calibration, retrieval scoring | Built. |
| Mechanical support check | Built. Grades from `support_literal`, consumes no model call (S-6). |
| Three verdict axes | Built: conclusion, summary, prohibited conclusion. |
| Retention controls | Built: cacheless provider path, no query text in the report, traced-provider run refused. |

### Blocking — NONE. The pass ran 2026-08-30.

Every item below was closed before the pass. They are kept because they record why
the sequencing was what it was, not because anything is outstanding. The record of
the run is `docs/handoffs/2026-08-30-rag-eval-graded-pass-run-record.md`.

One blocker appeared that this list never named: the grading model wraps its JSON
in a markdown fence, which sent every axis to `human_review` through a bare
`json.loads`. Caught by the Phase B smoke, fixed in PR #119 before the pass. No
fake could have surfaced it -- a fake returns the bare JSON its author wrote.

### Blocking (as it stood before 2026-08-30)

1. **~~The evaluator does not exist.~~ BUILT and merged 2026-08-29 (PR #111), NOT WIRED.**
   `rag_eval/evaluator.py` is on `main`, held by the blocking `rag-eval-gate`. Nothing
   calls it: `run.py` builds no `GradingCase`, so every summary and prohibited verdict
   still reports `not_evaluated`. The blocker moved from "the module is missing" to "the
   call site is missing" — a Titan pass today still produces retrieval numbers only.
   This is the one remaining build item before the graded pass.
2. **~~The grading-scope decision is unmade.~~ DECIDED (2026-08-28): retrieval plus the
   support verdicts.** Not retrieval alone. S-7 allows no retry for a bad result, so this
   is fixed for the pass. Three consequences follow, and they reorder the work:

   - **The pass cannot be spent before the evaluator exists.** Under retrieval-alone the
     Titan run could have gone today and produced a real number. It cannot now:
     `summary_verdict` has no mechanical producer at all, so a graded-on-support pass run
     today would report the support arm as `not_evaluated` and burn the one pass.
   - **`support_literal` becomes a hard prerequisite, not a nicety.** It is what keeps the
     four mechanical cases off a model call (S-6), and without it every support rate
     renders `n/a` — see Blocking 3, now done.
   - **Two rates, never one.** S-1 refuses a merged verdict by name, and S-4 requires the
     report broken out by topic rather than pooled. The graded result is retrieval metrics
     plus `conclusion_verdict` and `summary_verdict` rates, per topic.

3. **~~`support_literal` is absent from the gold set.~~ DONE 2026-08-29.** The four
   literals (`q01` "30 days", `q03` "12 CFR 1002.9", `q04` "25 months", `q24` "36 percent")
   are merged into `gold_v2.json`. A TF-IDF run confirms it: the expected-conclusion arm now
   reports **4 supported of 4 graded**, with the other 24 correctly `not evaluated` pending
   the evaluator. Backup of the pre-merge file sits beside it as `gold_v2.json.bak-*`.
4. **~~The generator model id and AWS region are unrecorded.~~ CLOSED — the client named
   both.** The approved configuration is:

   | Field | Approved value |
   |---|---|
   | Generator (evaluator) model id | `us.anthropic.claude-haiku-4-5-20251001-v1:0` |
   | Embedding model id | `amazon.titan-embed-text-v2:0` |
   | Embedding dimension | 1024 |
   | AWS region (both) | `us-east-1` |

   The two embedding values already match the code exactly — `_DEFAULT_BEDROCK_MODEL`
   (`rag_eval/run.py:464`) and `DEFAULT_EMBED_DIMENSIONS` (`rag_eval/embedder.py:125`).
   Nothing to change on the embedding side. The report can now state model ids and
   regions.

5. **~~Two client records disagree about the evaluator reading policy text.~~ CLOSED —
   the generator is on Bedrock, in the same account and region as Titan.** Naming a
   Bedrock generator model id settles which data path the run relies on: policy text goes
   to Bedrock under the existing approved AWS grant, not to an outside vendor. The
   2026-08-25 "sending policy text to a generative language model — Not approved" line
   and S-5's narrow exception are reconciled by the client choosing the model herself.
   **The report must still state the data path**, naming Bedrock, the region, and both
   model ids, so her stop condition is visibly satisfied rather than assumed.

### Not blocking, but worth closing first

- **The `us.` prefix on the generator id is a cross-region inference profile, not a
  region pin.** `us.anthropic.claude-haiku-4-5-20251001-v1:0` is called *from*
  `us-east-1`, but Bedrock may serve the request from any US commercial region in that
  profile. "AWS region (both): us-east-1" is therefore accurate as the calling region and
  overstated as a residency claim. Either word the report as the calling region, or ask
  the client whether she wants the profile-free id pinned to `us-east-1` instead. The
  Titan half has no such ambiguity — `amazon.titan-embed-text-v2:0` carries no profile
  prefix.
- **The credential is a Bedrock-only API key, and it needs no code change.** Verified
  against the installed packages: `bedrock-runtime`'s signing name is `bedrock` and its
  auth list carries `smithy.api#httpBearerAuth`, and botocore resolves a bearer token from
  `AWS_BEARER_TOKEN_<SIGNING_NAME>` (`botocore/utils.py:3626`, mapped to `BearerAuth` at
  `botocore/auth.py:1202`). So exporting `AWS_BEARER_TOKEN_BEDROCK` is enough —
  `BedrockEmbedder` passes no explicit credentials (`embedder.py:205`), so botocore picks
  the token up on its own. boto3/botocore 1.43.23 is what is installed. Same token covers
  the evaluator: Haiku and Titan are both `bedrock-runtime`, one signing name, one
  credential to explain in the report.

  On the authorization: a Bedrock-scoped key is **narrower** than the broad AWS credentials
  her approval already covers, and "broader AWS access" is what her Not-approved list names.
  It is still a different credential than the existing server-side setup, so record it in
  the report — but it tightens the posture rather than widening it.
- **~~Does the prohibited axis ride along?~~ DECIDED 2026-08-29: yes, graded.** Three graded
  axes, three rates, never merged. Measured cost and caveats below.
- Abstention is 1 of 6 and no threshold fixes it. Answerable top-1 scores span
  0.1464-0.3555 and abstention spans 0.1269-0.3561, so the highest abstention case
  outranks every answerable one. An exhaustive cutoff sweep floors at 5 errors of 23 —
  exactly what the calibrated 0.1367 already achieves. This is a property of the corpus,
  not a tuning target.

---

## 2. Setup, step by step

### Step 1 — Install the optional provider dependency

```bash
pip install -r rag_eval/requirements-bedrock.txt
```

`boto3` is imported lazily and only by the Bedrock backend, so the default TF-IDF path
and the whole test suite stay stdlib-only and keyless. Do not add this to CI.

### Step 2 — Set the backend, the region and the models

```bash
export RAG_EMBEDDER=bedrock
export AWS_REGION=us-east-1
export RAG_BEDROCK_MODEL=amazon.titan-embed-text-v2:0   # optional; this is the default
# The evaluator (not yet built — see §4). Client-approved id, pinned to a dated version.
export RAG_JUDGE_MODEL=us.anthropic.claude-haiku-4-5-20251001-v1:0
```

Both model ids and the region are the client's approved values, and the embedding pair
already matches the code (`run.py:464`, `embedder.py:125`). Pin the judge id as written —
a dated version, never a floating alias — because S-7 gives one bounded pass and a model
that moves under the run cannot be reported.

`AWS_REGION` is **required and explicitly refused when blank**. Passing `region=None`
would let boto3 discover a region from ambient host config, and Bedrock model access is
granted per region — a discovered region silently changes which account grant the run
depends on. Her authorization lists region probing under Not approved, so the refusal is
the control, not a convenience.

Blank counts as unset for both variables, because compose passes `${RAG_EMBEDDER:-}`,
which sets the name to `""` rather than omitting it.

### Step 3 — Provide the credential without a key literal

The credential for this run is a **Bedrock-scoped API key**:

```bash
export AWS_BEARER_TOKEN_BEDROCK=<the Bedrock key>     # never a literal in a file
```

`botocore` resolves a bearer token from `AWS_BEARER_TOKEN_<SIGNING_NAME>`, and
`bedrock-runtime` signs as `bedrock` with `smithy.api#httpBearerAuth` in its auth list, so
this variable alone authenticates the client. `BedrockEmbedder` passes no explicit
credentials, so **no code change is needed** — and the same token covers the evaluator,
since Haiku and Titan are both `bedrock-runtime` calls.

The alternative path still works unchanged: boto3 also resolves SigV4 credentials from the
environment, a profile, or an instance role. Either way, no key literal anywhere —
`secret-scan` blocks leaked literals and any tracked `.env`, and the `.dockerignore`
assertions in `test_rag_eval_seam.py` keep `.env` out of the origination build context,
which matters because `rag_eval/` ships inside that image.

A Bedrock-scoped key is narrower than the broad AWS credentials her authorization already
approves — her Not-approved list names *broader* AWS access. It is still a different
credential than the existing server-side setup, so name it in the report.

### Step 4 — Confirm tracing is off

```bash
env | grep -i langsmith        # expect LANGSMITH_TRACING unset or false
```

A provider-backed run with `LANGSMITH_TRACING` on is **refused before any call**. The
LangSmith singleton's `hide_inputs`/`hide_outputs` hardening lives in origination-service
and runs at service startup, which an offline harness never reaches — and that hardening
deliberately does not hide errors. Nothing the report needs comes from a trace, so the
safe configuration is no tracing.

### Step 5 — Confirm the packet is intact

```bash
cd <packet dir> && shasum -a 256 -c SHA256SUMS.txt      # expect 18 OK
```

If a previous run left `rag_eval/` inside the packet, delete it first — the run writes its
report and cache into `--base`, which changes the packet's file count.

### Step 6 — Pick the right manifest

Two ship, scoped differently. Picking the wrong one reports "manifest declares no approved
files", which reads like a contaminated corpus:

| Manifest | Names are relative to | Use with |
|---|---|---|
| `SHA256SUMS.txt` | the package root (`policies/X.md`) | `--base <packet>` |
| `CORPUS-SHA256SUMS.txt` | the corpus directory (`X.md`) | `--base <packet>/policies` |

`CORPUS-SHA256SUMS.txt` is itself unhashed by `SHA256SUMS.txt` and absent from the
inventory. They agree today; nothing enforces that they keep agreeing.

### Step 7 — Dry-run on TF-IDF first

```bash
unset RAG_EMBEDDER
python3 -m rag_eval.run \
  --base <packet> \
  --gold <gold set> \
  --manifest <packet>/SHA256SUMS.txt \
  --displayed-summaries-manifest <summaries dir>/SHA256SUMS.txt
```

Pass BOTH manifests, the same as step 8. An earlier version of this step passed only
`--manifest`, which left the summaries audit unexercised until the graded pass — a
failure mode that is not Titan-specific, in the one step that exists to catch those.

Keyless, cached, free. It proves the corpus admits, the gold set loads, every guard
passes and the report renders — every failure mode that is not Titan-specific. Spend the
graded pass only after this is clean.

**Run 2026-08-29, clean.** 6 files scanned / 0 refused; 66 chunks; threshold
0.13666298135750454; embedder `tfidf-v1:a4f3039100df155d`; corpus `corpus-0b32d4ca92a5`.
Support test: conclusion 4 supported / 24 not evaluated, summary 0/28, prohibited 0/28 —
the four are the `support_literal` rows q01, q03, q04, q24. The per-topic verdict table
renders all eight topics with three columns. Retrieval: 5 false-confident abstention
cases (q18–q21, q23) and 0 wrong abstentions, which the calibration section already
reports as the minimum-error point for this corpus rather than an untuned parameter.
Packet re-verified at 18 OK after `rm -rf <packet>/rag_eval`.

That run also settles S-6 empirically: `apr_finance_charge` holds exactly one case, q24,
and q24 is literal-backed. The whole-case reading of S-6 would drop it from the model
entirely, leaving that topic `n/a` on all three axes — S-4 keeps all eight in scope, so
conclusion-axis-only is the reading that survives.

**Re-run 2026-08-30 on the merged tree (`e5f6ea7`), with the exact command line the
graded pass then used, minus the two provider variables.** Identical to the 08-29
figures: 6 files scanned / 0 refused, 66 chunks, threshold 0.13666298135750454,
embedder `tfidf-v1:a4f3039100df155d`. So #116 and #119 changed nothing about
admission or retrieval. It also proved the four flag names and four paths, and
confirmed validation runs before the first embedding call -- a bad manifest or path
fails the run before a provider call is spent. Packet restored to 18 OK afterwards.

### Step 8 — The graded pass

```bash
export RAG_EMBEDDER=bedrock
export AWS_REGION=<region>
python3 -m rag_eval.run \
  --base <packet> \
  --gold <gold set> \
  --manifest <packet>/SHA256SUMS.txt \
  --displayed-summaries-manifest <summaries dir>/SHA256SUMS.txt
```

Record from the run's own output: the threshold it prints, the embedder signature, the
provider call count, the retry count and the input-token total. Those counters are what
the post-run report reads; the cache counters are structurally zero on this path and
describe nothing.

**Ran 2026-08-30.** Embedder `bedrock-v1:amazon.titan-embed-text-v2:0:1024`; 94
provider calls, 0 retries, 6,644 input tokens, cache disabled; judge
`us.anthropic.claude-haiku-4-5-20251001-v1:0`, 28 calls; threshold recalibrated to
**0.3007877649147387**. Retrieval: 17 answerable (hit@1 0.71, hit@3 0.88, hit@5
0.94, MRR 0.80), 6 unanswerable all correctly below threshold, 5 clarification
unscored. All three axes 1.00; 13 verdicts to `human_review`, of which every one is
the 240-character rationale cap firing, not evaluator uncertainty (run record, S7).

### Step 9 — Clean up and re-verify

```bash
rm -rf <packet>/rag_eval
cd <packet> && shasum -a 256 -c SHA256SUMS.txt      # expect 18 OK again
```

Never commit the packet or the summaries package. The repository is a public fork, and
her approval covers indexing, not publishing.

### Step 10 — Re-derive the retrieval threshold

The threshold in `.env.example` was calibrated for the committed corpus under TF-IDF.
Switching the backend invalidates it. Use the value the Titan run prints, and record it
with its `(corpus_digest, embedder_signature)` pair. **Derived 2026-08-30: `0.3007877649147387`**, bound to (corpus of 66 chunks from
`<packet>`, embedder `bedrock-v1:amazon.titan-embed-text-v2:0:1024`).
At that value 2 answerable cases would wrongly abstain and 0 unanswerable retrieve
with false confidence -- against TF-IDF's 0 and 5 on the identical corpus the same
day. `POLICY_RETRIEVAL_MIN_SCORE` still
has **no committed default** — unset stays the operational kill switch (ADR 0019).

---

## 3. The pipeline

### A. Top level

```
  --manifest          --displayed-summaries-manifest        --gold
      |                          |                             |
      v                          v                             v
 [1 FREEZE the summaries package]  ...runs FIRST, before any retrieval
      |
      v
 [2 ADMIT the corpus]  manifest audit, both directions
      |
      v
 [3 GATE]  scan_file per candidate -> passed / REFUSED
      |
      v
 [4 CHUNK]  gate-passed .md only -> doc#section-slug   (66 chunks, her packet)
      |
      v
 [5 LOAD GOLD]  shape -> aliases -> keys -> per-row -> PII guards
      |
      v
 [6 EMBED]  fit + embed chunks, embed each query
      |
      v
 [7 RETRIEVE]  InMemoryIndex.search, top-k cosine
      |
      v
 [8 CALIBRATE]  threshold from the top scores just produced
      |
      v
 [9 SCORE]  retrieval hits/RR/correct  +  3 verdict targets
      |
      v
 [10 REPORT]  eval_report.md  (counts, ids, verdicts, 1 rationale line)
```

### B. Stages 1-3, the fail-closed points

```
 freeze summaries ---- problems? ---> ABORT
        |                             (a package that changed is not the approved one)
        v
 load_corpus_manifest --- unparseable? ---> ABORT
        |                                   (never degrades to an empty allowlist —
        v                                    that would read as "corpus contaminated")
 audit_corpus_against_manifest
        |
        +-- manifest entry missing on disk ---> ABORT
        +-- unlisted file present -----------> ABORT  (reported by POSITION, never by
        |                                             name — a name can be the PII)
        +-- digest mismatch -----------------> ABORT
        v
 per-file admission: unsafe_corpus_path_reason
        |
        +-- "pii" / "non-slug" / "not-in-manifest" / "manifest-digest-mismatch"
        |        -> REFUSED, never chunked
        v
 scan_file (content)  -> passed | REFUSED
        |
        +-- kb_dump/applications.jsonl: refusal EXPECTED, pinned by sha256
```

### C. Stage 5, gold load

```
 --gold file
     |
     v
 _load_gold_queries    one JSON doc {"queries": [...]}
     |                 JSONL, or no 'queries' key -> ABORT naming the SHAPE
     v
 alias normalize       acceptableConclusion            -> expected_conclusion
     |                 prohibitedUnsupportedConclusion -> prohibited_conclusion
     |                 sourceDocument/sourceHeading    -> source_document/heading
     |                 both spellings on one row -> ABORT (no silent precedence)
     v
 unknown key? -> ABORT           closed allowlist, fails closed
     |
     v
 per-row: id slug, non-empty query, expected = chunk ids,
          outcome_class in 4, no_match => unanswerable & no expected,
          clarification => no anchor, support pair both-or-neither
     |
     v
 per-file: support pair on SOME rows but not all -> ABORT
     |
     v
 PII guards ----- scan_text (labelled: SSN/PAN/email/phone/bank/DOB)  on ALL fields
     |
     +------------ person-name heuristic  on free text ONLY
                     exempt: source_document, source_heading   (structured anchors)
                     exempt: expected_conclusion, prohibited_conclusion
                             (never embedded, never reported -> no exposure to guard)
     |
     v
 anchors resolve: source_document + source_heading -> chunk id, under the ACTIVE
                  admission mode (ids are not stable across modes — this is the
                  only bridge between her filenames and digest-derived doc ids)
```

### D. Stages 6-8

```
 make_embedder  (RAG_EMBEDDER)
     |
     +-- refuse_traced_provider_run: LANGSMITH_TRACING on + provider -> ABORT
     |
     +-- cache_enabled(embedder)?
             TF-IDF  -> cache ON   <base>/rag_eval/.cache/embeddings.json
             Titan   -> cache OFF  and any existing cache file is UNLINKED
     v
 fit(chunk texts) -> embed each chunk -> InMemoryIndex.add
     |
     v
 embed each gold query  <-- the ONLY gold field that reaches the provider
     |
     v
 index.search(k=5) -> [(chunk_id, score), ...] per query
     |
     v
 calibrate_threshold(answerable tops, abstention tops)   exhaustive cutoff sweep
```

The threshold is calibrated **after** retrieval, from that run's own scores. It is not an
input you set.

### E. Stage 9, three targets on three axes

```
 RETRIEVAL           answerable    -> expected chunk in top-k?  hit@1/3/5, RR
   (correct: bool)   abstention    -> top score BELOW threshold?
                     clarification -> UNSCORABLE, leaves every denominator

 SUPPORT x2          conclusion_verdict   supported / unsupported / human_review
   (never merged)    summary_verdict      / not_evaluated
                     produced by: mechanical grep of support_literal in the passage
                     rate = supported / (supported + unsupported)

 PROHIBITED x1       prohibited_verdict   avoided / asserted / human_review
   (own polarity)                         / not_evaluated
                     produced by: NOTHING YET — prose, no literal to match
                     rate = avoided / (avoided + asserted)
```

### F. What survives the run

```
 chunk vectors    TF-IDF -> disk cache      Titan -> discarded at process exit
 query vectors    same
 query text       never written (S-10) — the report cites {query_id} only
 conclusion text  never written, never embedded
 eval_report.md   the only artifact:  case ids, topic, source-section ref,
                  verdicts, one rationale line
```

Nothing persists a Titan vector. That is a retention control, not an oversight: an
embedding is content-derived, and her rule excludes retention of retrieved content. The
consequence to plan around is that a rerun re-embeds everything from scratch.

Persisting them is the D16 / pgvector work, and it is not just plumbing — it needs an
ADR 0007 rule 6 amendment, a PII re-review of a now-persistent store, and her
authorization, which today covers indexing rather than retention.

---

## 4. The evaluator: scope, exposure, cost

Not built. This section is what it has to be, sized against the actual gold set so the
build is not scoped from intent.

### A. What it grades

Three axes over 28 cases. Retrieval is never judged — it is scored mechanically, and the
evaluator cannot touch it or the threshold.

| Axis | Judged rows | Why |
|---|---|---|
| `conclusion_verdict` | 24 | `q01`, `q03`, `q04`, `q24` carry `support_literal` and resolve mechanically. S-6 forbids a model call on them. |
| `summary_verdict` | 28 | The displayed summary describes a passage rather than asserting a literal, so no mechanical check applies. |
| `prohibited_verdict` | 28 | Her prohibition is prose and carries no literal. |

Output per case: three enum verdicts plus **one** rationale line (S-8, S-10). Never an
edit to a conclusion, a summary or a decision.

**Graded scope (decided 2026-08-28/29): retrieval plus the support verdicts, and the
prohibited axis rides along as a third graded axis.** Three rates, reported per topic (S-4)
and never merged into one number (S-1). See Blocking 2 in §1 for what that reorders.

### A2. How S-6 is read — DECIDED (2026-08-28): the conclusion axis only

S-6 says "the four mechanical cases must not consume a model call." Read whole-case, that
is 72 gradings and `q01`/`q03`/`q04`/`q24` stay `not_evaluated` on two axes. Read
axis-only, it is the 80 in the table above. **Axis-only**, for four reasons, the first
decisive:

1. **Whole-case empties a topic.** `q24` is the only `apr_finance_charge` row and is one of
   the four mechanical ones, so whole-case exclusion leaves that topic with n=0 on the
   summary axis. S-4 keeps all eight topics in scope and requires the report broken out by
   topic; whole-case silently drops one of them from an axis the graded scope now includes.

   ```
   topic                   all   if whole-case excluded
   adverse_action            8                        6
   apr_finance_charge        1                        0   <--
   credit_decisioning        5                        5
   debt_to_income            1                        1
   eligibility_rules         7                        7
   fee_schedule              1                        1
   interest_rate             1                        1
   records_retention         4                        3
   ```

2. **The two rates would otherwise describe different populations** — conclusion over 28,
   summary over 24. S-1 keeps the verdicts distinct, which is not the same as measuring
   them over different case sets, and the four dropped rows are exactly those carrying
   load-bearing literals ("30 days", "25 months", "12 CFR 1002.9") rather than a random
   four.
3. **The built exclusion is already axis-scoped.** `_support_verdict` (`run.py:1148`) reads
   `support_literal` and sets `conclusion_verdict` alone. Whole-case would be a new, wider
   reading than the code that shipped.
4. **The exclusion has nothing to bite on elsewhere.** A displayed summary asserts no
   literal, so no mechanical result exists for a model call to redundantly re-derive. S-6
   stops a model re-doing what a `grep` already did; on the summary axis no `grep` ran.

**This is an interpretation of her instruction, and S-7 makes it unrevisable after the
pass — so it goes in the report explicitly**, in one line: the four mechanical cases consume
no model call *on the axis their literal decides*, and are graded by the evaluator on the
two axes no literal covers.

### A3. What the prohibited axis costs, and its caveats

**Cost: negligible, on one condition.** Her prohibited text is 2,376 chars over all 28 rows
— 84 chars, about 21 tokens, per case, so ~594 input tokens for the whole run plus a few
hundred output. Under a thousand tokens added to a run already estimated at ~40K. Fractions
of a cent at Haiku rates.

**The condition is batching.** That figure holds only if the three axes share one prompt per
case. One call per axis instead adds 28 calls — still cents, but it changes the shape of the
run, and S-7 counts a pass, not a dollar.

**Caveat 1 — polarity inversion is the real risk, and batching is what invites it.** Two axes
where `supported` is the good outcome sit in the same prompt as one where `avoided` is. That
is exactly the confusion `metrics.py` already warns about in prose: printing "the prohibited
conclusion is supported" reads as the opposite of the finding. S-7 allows no retry for a bad
result, so an inverted verdict is unrecoverable. Mitigations, both cheap: name the axes by
their own state words in the prompt (never "supported" for the prohibited axis), and have the
evaluator emit the enum strings themselves rather than a boolean the caller maps.

**Caveat 2 — a gap this decision surfaces, and it is not prohibited-specific.** S-4 requires
reporting **by topic, not pooled**, but per-topic reporting today is *retrieval only*:
`TopicStat` carries `n` and `correct`, and the report's topic table is
`| Topic | Cases | Correct |`. All three verdict axes are pooled over the whole set
(`_support_stat(e.conclusion_verdict for e in evals)` and its two siblings). Now that the
verdicts are inside the graded result, **per-topic verdict rates have to be built** — fold it
into step 3 with the `rationale` field, not into the evaluator.

**Checked and clean, no action needed:**

- All 28 rows carry a prohibited conclusion, so no row is ungradeable on this axis.
- `_UNECHOED_TEXT_KEYS` covers `prohibited_conclusion`, and `report.py` reads no gold text
  field at all — S-10 holds.
- The numerator contract is already pinned by a test:
  `test_prohibited_conclusion.py:191` asserts `PROHIBITED_STATES[0] == AVOIDED`, so a
  reorder that would silently invert the rate fails the suite.
- The report already renders the prohibited section as its own axis with its own states.

### B. Determinism

Deterministic today: retrieval scoring and ranking, `calibrate_threshold` (exhaustive, not
sampled), `corpus_signature`, the four mechanical verdicts (normalized substring,
deliberately not fuzzy), and TF-IDF embeddings.

Not deterministic: **the evaluator**, and it is the entire non-deterministic surface. S-7
leaves it unhedged — one sample per grading, retry on technical failure only, no majority
vote, no re-roll on a verdict you dislike. Two levers, both set before the pass: a dated
model id (above) and `temperature: 0`. Haiku 4.5 still accepts sampling parameters, so
temperature is actually settable on this model — it does not make a generative judge
deterministic, but it removes the cheapest source of variance.

Titan is not sampled, but the provider path is cacheless, so a rerun re-embeds rather than
replaying stored vectors. A silent model-version change would move the threshold with
nothing on disk to compare against. That is why the run is bounded to one pass.

### C. What leaves the machine

Per graded case: the top-5 retrieved passages (`max(K_VALUES)`), the officer question, the
expected conclusion, the prohibited conclusion, and the frozen displayed summary. Across 28
cases against only 66 distinct chunks, effectively the whole ~31KB policy corpus goes out.

**There is no redaction step on that path.** `hygiene.scan_text` is an admission gate, not
a scrubber — it refuses a corpus carrying SSN, Luhn-valid PAN, EIN, IBAN, bank, CVV, email,
phone, DOB-context, or labeled name and address. The control is "the corpus was admitted
clean", not "PII is stripped en route".

**Gap to record before building:** `_UNECHOED_TEXT_KEYS` (`expected_conclusion`,
`prohibited_conclusion`) sit out the person-name guard *because they are never embedded and
never reported*. The evaluator sends both to a model. That justification stops holding the
moment it exists — either re-scan those two fields or record the exemption as knowingly
widened.

### D. The product-path seam (C-6)

`rag_eval/` is **already on the product path**: compose builds origination from the repo
root, the Dockerfile does `COPY rag_eval ./rag_eval`, and the blocking `rag-eval-import-gate`
proves `import rag_eval` resolves inside the built origination image. `search_policy` is
built on that holding.

So "the evaluator lives in `rag_eval/`" does not by itself satisfy C-6 — it ships the judge
into the origination image. What needs enforcing: no `services/` module imports it, no
credential is read at import time, and it is never constructed inside a service process.
`services/origination-service/tests/test_rag_eval_seam.py` currently asserts the opposite
direction (that `rag_eval` is present); this needs its own assertion.

### E. Cost

Embeddings: 66 chunks + 28 queries = 94 Titan calls, ~8K tokens. Cents.

Evaluator, roughly 1.4K input tokens per case (~820 of data plus prompt scaffold) and ~80
out:

| Shape | Calls | Input | Output |
|---|---|---|---|
| one call per case, three axes per prompt | 28 (24) | ~40K | ~2.5K |
| one call per axis | 80 (72) | ~112K | ~8K |

Haiku 4.5 is the cheapest current tier and carries a 200K context, so a ~1.4K prompt is
nowhere near the ceiling. Bedrock is partner-priced separately from the first-party rates;
either way this is a sub-dollar run. **Cost is not the constraint — S-7 is.** Budget the
engineering, not the tokens.

### F. Build order

1. ~~Merge `support_literal` into the gold set~~ **DONE** — 4 rows, out-of-repo, no PR:
   q01 "30 days", q03 "12 CFR 1002.9", q04 "25 months", q24 "36 percent".
2. ~~Settle PR #108~~ **DONE** — rescued by PR #109 (`01f8cc1`).
3. ~~Add the `rationale` field and carry the verdicts through `QueryEval` / `Aggregate` /
   `report`.~~ **DONE** — merged as PR #110, plus the per-topic verdict table.
4. ~~The evaluator: the Bedrock judge client, S-7's three controls, its own tracing
   refusal, and the seam assertion.~~ **DONE** — merged as PR #111, with ADR 0022.
5. **Wire it (next).** Build a `GradingCase` per gold row from the passages the run
   actually retrieved, skip the conclusion axis on the four `support_literal` rows (S-6,
   read as the conclusion axis only — see A2), and assign the three verdicts plus the
   rationale onto `QueryEval`, truncating to `RATIONALE_MAX_CHARS`. Three items ride
   along, because they are false or stale the moment the evaluator can run:

   - `report.py:313` says a `not_evaluated` case is unevaluated because "the evaluator is
     not built". It is built. That sentence reaches the client in the deliverable, and it
     still has to render for a retrieval-only run, so it needs rewording — "did not run" —
     not deletion. Regression test with it.
   - ADR 0022's Status and implementation steps 2-3 still describe the wiring as blocked
     on `rationale`, which is on `main`. Same commit.
   - `CLAUDE.md` names one residual for 0022 ("not wired into the run"). It closes here.

`refuse_traced_provider_run` (`run.py:484`) reads `IS_PROVIDER_BACKED` off the **embedder**,
so it is blind to a judge client. The evaluator needs its own refusal on the same variable —
LangSmith stays off for the judge, by decision, not by default.
