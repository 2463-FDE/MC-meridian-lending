# Client asks — 2026-08-26 working log, Gate 0 data path + packet verification

**Audience:** Dana (VP Lending Ops, Meridian) · **Sent:** 2026-08-26 · **Answered:** 2026-08-26

**Status: SENT and ANSWERED — both flows APPROVED, with operating limits.** Her reply is
transcribed below. The as-sent email and the verbatim reply have landed on the
`docs/client-asks-originals` branch under this same filename, in commit `4514563`
(2026-08-26). That branch is local-only and deliberately unpushed, so inside this repository
as published, the transcription below is the authoritative record and there is nothing here
to check it against. She also confirmed the manager-escalation correction and set an ordered
precondition: **fix filename validation first**.

Three of her limits conflicted with the code as it stood on 2026-08-26 — see
"Constraint register" below; two of them needed answering before the run.

> **Currency note, 2026-08-30 — the run has since happened.** This is the pre-run log, and
> everything below is historical: the limits are the standard the graded pass was held to,
> not open work still gating it. The pass ran on 2026-08-30 against `origin/main` =
> `e5f6ea7` and its results are final, so "no Titan call has been made yet" was true when
> this was written and is not true now. The filename precondition she ordered first was met
> before the run, in PR #116 (`64f4871`). The two retention questions in "The retention
> conflict" below were closed by removing the retention rather than by her answer: the
> report and run record state that no prompt, passage or model response is retained and
> that the embedding cache is disabled on the provider path, so no content-derived vectors
> reach disk. For the asks that came out of the run see
> `docs/client-asks/2026-08-30-graded-pass-results.md`; for the measured results and the
> full run envelope see `docs/reports/rag-eval-graded-pass-2026-08-30.md` and
> `docs/handoffs/2026-08-30-rag-eval-graded-pass-run-record.md`.

**Follows** `docs/client-asks/2026-08-25-policy-corpus.md` — that file holds the original five asks
and her scoped approval. This one is the return round: two approvals we need, and the two
confirmations she asked for.

**Packet location.** Verified from a scratchpad copy, deliberately outside the repo. It is **not
tracked and must not be committed** — the repo is a public fork, and her approval covers indexing,
not publishing. Her own note is pointed about this: *"This authorization is not a verification that
the public repository currently enforces this data separation."*

## Packet verification, 2026-08-26

All 15 `SHA256SUMS.txt` entries verify. Inventory self-reports 19 regular files / 101,777 payload
bytes and matches.

| Delivered | Count | Verified |
|---|---|---|
| Synthetic Markdown training policies | 5 | 5/5 pass the ADR 0007 hygiene scan |
| Officer questions | 28 | load as JSONL; classes `answer` 12, `clarification` 5, `no_match` 6, `manager_escalation` 5 |
| Whole-document exclusion fixtures | 2 | both refused, by two different paths — see below |
| Chunks produced | **66** | vs 9 in the current corpus, 7.3× |
| Chunk size (chars, min/median/max) | 157 / 463 / 1170 | max ≈292 tokens — **no Titan truncation risk** |

## What we need approved

| # | Ask | Why it is outside her description | Dana's response |
|---|-----|-----------------------------------|---|
| 1 | Titan embedding of the **search query** at request time | Her text approves policy files leaving *"solely for that indexing or re-indexing."* `policy_retrieval.search()` calls `embedder.embed(query)` per search, which is a live AWS call outside indexing. The query is model-authored from the closed 8-code `POLICY_TOPICS` vocabulary — no borrower data, no officer free text — but the flow is not covered | **APPROVED** — *"query-time Titan embeddings for the fixed officer-topic list"*. Note her matching prohibition: **no free-form searches**, which the closed 8-code vocabulary already enforces |
| 2 | Titan embedding of **her 28 questions** during eval runs | Scoring retrieval embeds gold query text. Her own packet becomes outbound traffic. `gold_queries.json`'s existing privacy contract already records this for the Bedrock backend | **APPROVED** — *"embeddings for the 28 synthetic evaluation questions you supplied"* |

Neither is avoidable under a dense embedder: matching a query against documents requires embedding
the query the same way. If either is outside what she intended, the answer is to stop, not to work
around it.

## Confirmations volunteered in the same round

Three of her exclusions, confirmed against code rather than asserted:

- **No region probing** — `AWS_REGION` set explicitly. the `AWS_REGION` block in `.env.example` documents a us-east-1
  default on the bedrock path; that becomes required rather than defaulted.
- **No fallback models** — `make_embedder` raises on an unrecognised `RAG_EMBEDDER` rather than
  substituting a backend. One gap to close: an *unset* value silently selects TF-IDF, which under a
  Titan-calibrated threshold is wrong in a way nobody sees. Making that fail closed turns her
  requirement into a property.
- **No new credentials** — reuses the existing server-side setup (`docker-compose.yml`,
  already plumbed for the LLM Bedrock provider).

## Answers to her two questions

**Approved corpus boundary.** `POLICY_CORPUS_DIR` (`services/origination-service/app/config.py`)
points retrieval at exactly one directory. Set to the packet's `policies/`, the indexed corpus **is**
the approved packet: our two existing `policies/*.md` are excluded and nothing outside can be picked
up. Her scope note specifies the two exclusion fixtures sit *"outside the approved policy
directory"*, which matches the packet layout — they are tested, never indexed.

**No-match.** Built, and the default failure mode. Below-threshold, empty corpus, unset threshold,
refused file, unreadable file, harness-unavailable all return `policy_abstain`. Both fixtures behave
correctly, by two different mechanisms:

| Fixture | Result |
|---|---|
| `PLACEHOLDER-NOT-A-PERSON-file-notes.md` | content REFUSED — `{'ssn': 1, 'dob': 1, 'bank': 1}` |
| `PLACEHOLDER-PERSON-ALPHA-ssn-000-00-0000.md` | content passes, **name** refused (`pii`) |

The second is the harder case and the one worth showing her: clean content, identifying filename,
caught only by the path check.

**Manager-escalation — a correction, not a confirmation.** Not a system behaviour. If a document
says to refer to a manager, the assistant retrieves that passage and quotes it verbatim. Nothing is
routed, nobody is notified, no escalation is recorded. If her five `manager_escalation` questions
assume routing, they test something the platform does not have. Said plainly now rather than
demonstrated as a passing result that means less than it looks like.

## Volunteered defect — why it is in the email

Our own `_SAFE_FILENAME` rule (`rag_eval/run.py`, lowercase-only) currently rejects **all five**
of her documents as `non-slug`, so `_load_corpus` skips every one and the corpus is empty. A Titan
run would have produced a working-looking system answering "no policy match" to everything, silently
— `log.warning` only. Ours to fix.

It goes in the email because she explicitly declined to vouch for our implementation. Reporting a
defect we found by checking is worth more than a clean report she has no reason to believe.

## Draft email — NOT what was sent

_The email body for this section is held outside this repository with the other client-facing copies. This log keeps the reasoning, the decisions and her replies; the sent text is not published here._

## Not in the email, deliberately

- **The eight topic codes do not fit her corpus.** Measured against the packet with TF-IDF, five of
  eight retrieve the wrong document and three clear the current 0.1609 threshold on lexical
  overlap: `fee_schedule` lands on `CREDIT-UNDERWRITING#evaluation-process-and-confidential-cutoffs`
  (0.3776), `interest_rate` on `SERVICING#servicemembers` (0.1945), `eligibility_rules` on
  `SERVICING#purpose` (0.2887). Her packet has no pricing or APR content and withholds cutoffs by
  design, so `fee_schedule`, `apr_finance_charge`, `interest_rate` and `debt_to_income` have no home
  in it. **And `test_policy_topic.py` still passes, because it asserts a hit exists, not a correct
  one** — so `agentic-loop-gate` goes green on a fee question answered with underwriting cutoffs.
  Ours to fix; not her decision, and not worth her attention until it is.
- **The gold-schema mismatch.** Her rows carry `acceptableConclusion` and
  `prohibitedUnsupportedConclusion` — she grades the *conclusion*; our harness grades the *retrieved
  chunk*. Retrieving the right passage and stating the wrong deadline passes today. Whether we build
  conclusion-grading is an internal scope decision, not an ask.
- **Retrieval noise.** 40 of 66 chunks are scaffolding repeated across all five documents
  (`#labels`, `#purpose`, `#scope`, `#roles`, `#records`, `#escalation`, `#review-cadence`,
  `#_intro`). Two of the wrong hits above landed on `#purpose`. Internal.

---

## Her reply — transcribed 2026-08-26

> Thanks for checking before making the calls - you were right that these go beyond the original
> indexing approval.
>
> Yes, both flows are approved: query-time Titan embeddings for the fixed officer-topic list, and
> embeddings for the 28 synthetic evaluation questions you supplied.
>
> Please fix filename validation first, and confirm the corpus contains exactly the five approved
> policy files, is non-empty, excludes your existing sample documents and both exclusion fixtures,
> and preserves the verified checksums unchanged.
>
> Keep the run bounded to one full pass, plus one correction rerun if needed, with no more than two
> attempts per item and no more than $1 total Titan cost. Use only the existing Titan model, the
> configured region, server-side credentials, and current permissions. No fallback model, free-form
> searches, borrower data, real internal policy, production traffic, new permissions, or retention of
> query text, questions, retrieved content, identifiers, credentials, or raw provider errors.
>
> Your manager-escalation interpretation is correct: return the relevant policy text only; do not
> route, notify, trigger a workflow, or create an action.
>
> Stop and check with me if any input, checksum, exclusion, model, region, logging, call, cost, or
> permission boundary changes. After the run, send the commit SHA, model and region used, call and
> retry counts, cost, corpus and exclusion results, retrieval results, and logging confirmation.

## Constraint register — her limits against current code

| Her limit | Current code | Status |
|---|---|---|
| Fix filename validation **first** | `_SAFE_FILENAME` (`rag_eval/run.py`) is lowercase-only; all five documents refused | **ordered precondition** |
| Corpus = exactly the five files, non-empty, excludes our samples and both fixtures | `POLICY_CORPUS_DIR` gives the boundary; nothing asserts it | build the assertion |
| **Preserve the verified checksums unchanged** | settles the filename question: **renaming her files is forbidden**, so the regex must change | decided |
| No more than **two attempts per item** | `BedrockEmbedder` sets no `botocore.config.Config`; boto3's default retry count exceeds two | **CONFLICT — must pin `max_attempts`** |
| One full pass, plus one correction rerun | the product path re-embeds the whole corpus **per process** (`_build_index()` does not use `EmbeddingCache`); every restart is another full pass | **CONFLICT — see below** |
| No retention of query text, questions, retrieved content | two sites in `rag_eval/report.py` write query text verbatim into `eval_report.md`, and two in `rag_eval/run.py` write `rag_eval/.cache/embeddings.json` | **CONFLICT — must resolve before the run** |
| No retention of raw provider errors | `BedrockEmbedder.embed` has no `except`; a botocore error propagates and may be logged verbatim upstream | **CONFLICT — must wrap** |
| No fallback model | `make_embedder` raises on an unknown backend. Gap: an **unset** value silently selects TF-IDF | close D-10 |
| Existing model, configured region, server-side credentials, current permissions | `AWS_REGION` explicit (no probing), creds already plumbed at `docker-compose.yml`, no new grants | satisfied |
| No more than $1 total Titan cost | ~66 chunks + 28 questions + 8 topic queries ≈ 25k tokens — orders of magnitude under $1. Not the binding constraint; report the actual | satisfied |
| Manager-escalation: text only, no routing | nothing routes, notifies, or records an escalation | **confirmed correct by her** |

### The pass-count conflict, stated plainly

"One full pass, plus one correction rerun" bounds us to at most two indexing passes. The running
service re-embeds all 66 chunks on the first search **in every process**, so bringing the interactive
stack up under Titan spends a pass per restart and breaches the bound within minutes.

Resolution that needs no new approval: perform the graded run through the harness as a controlled
one-shot, and **do not bring the interactive stack up under Titan** until we have asked. The
alternative — persisting vectors so the cost is paid once — collides with her no-retention rule and
with D16, so it is not available.

### The retention conflict

Three writers retain content she has excluded: the eval report writes query text verbatim, the
embedding cache writes content-derived vectors to disk, and an unwrapped provider exception can put
a raw error into logs. LangSmith is a fourth surface — framework spans carry the policy query and
prose unless the singleton is primed `hide_inputs`, and `LLM_TRACE_CONTENT` must stay false.

Her stop-boundary explicitly names **logging** as a trigger, so this is not ours to interpret
quietly. Two of these need a decision from her rather than a workaround from us: whether a report
without question text still serves her purpose, and whether vectors count as retained content.

## Claims-to-make-true, promoted out of step 4

The sent email made four present-tense claims that code does not yet satisfy. Her reply asks us to
confirm three of them in the post-run report, so they are commitments, not niceties, and they move
ahead of the threshold work:

1. **"AWS region is explicitly configured"** — compose passes `AWS_REGION: ${AWS_REGION:-}`, and
   the `AWS_REGION` block in `.env.example` documents a us-east-1 default on the bedrock path. Make it required.
2. **"no fallback models are used"** — an unset `RAG_EMBEDDER` silently selects TF-IDF. Fail closed
   (D-10). This is a correction to a stated claim.
3. **"Retrieval is limited to the five policy files"** — `POLICY_CORPUS_DIR` is not set in compose
   and nothing asserts the contents.
4. **"The query comes from a fixed set of policy topics"** — `search_policy` takes model-authored
   free text (`assistant.py`). Either send the topic code as the query, or correct the
   representation with her. Her approval mirrors our wording and separately forbids "free-form
   searches", so this is a representation question, not a preference.
