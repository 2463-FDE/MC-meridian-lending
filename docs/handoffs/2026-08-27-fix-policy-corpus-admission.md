# Handoff — policy-corpus ingestion for the client's synthetic packet (2026-08-27)

**Branch:** `fix/policy-corpus-admission` · **Base:** `origin/main` · **Repo:** `/Users/maha/Desktop/revature/MC-meridian-lending`
**Status:** Ingestion and controls shipped and pushed, both PROVEN. The graded Titan run is
blocked on client-side work (query generation → human review → freeze) and on our own
topic-mapping work. **The correction email to Dana was sent on 2026-08-27** and is recorded on
both client-record branches as `docs/client-asks-2026-08-27-support-test-correction.md`; her
answer on the support test is outstanding.

## Where the client interaction lives — read this first

Every exchange with Dana is recorded on two branches. Neither is pushed except
`docs/client-asks`, which is 33 commits ahead of its remote.

- **`docs/client-asks-originals`** — **verbatim** emails, both directions. Never edit, summarise
  or reorder these. This is the record of what was actually said.
  - `docs/client-asks-2026-08-25-policy-corpus.md` — her packet reply + the packet's own
    `titan-embeddings-authorization.md` and scope confirmation, reproduced in full
  - `docs/client-asks-2026-08-26-gate0-data-path.md` — the as-sent Gate 0 email and her
    approval of both Titan flows with operating limits
  - `docs/client-asks-2026-08-26-query-composition.md` — the query-composition round and her
    generate-review-freeze decision
  - `docs/client-asks-2026-08-27-pre-run-disclosures.md` — the pre-run email as sent, with the
    one contradicted claim flagged at the top
- **`docs/client-asks`** — the derived reading for each of the above: constraint registers,
  what each limit means in code, and what is still owed. Same filenames.
- `docs/eval-design-inputs-2026-08-26.md` (on `docs/client-asks`) — the decision ledger:
  ten settled decisions D-1…D-10, five open O-1…O-5, and nine constraints the eval design has
  to satisfy. **Start here for the "why" behind any of the code below.**

**The client records are on other branches, so read them without switching** — the working tree
is routinely dirty and a concurrent session may hold it:

```bash
git show docs/client-asks-originals:docs/client-asks-2026-08-27-pre-run-disclosures.md
git show docs/client-asks:docs/eval-design-inputs-2026-08-26.md
git ls-tree -r --name-only docs/client-asks -- docs/ | grep client-asks   # list them all
```

Both branches are **local-only** except `docs/client-asks`, which is 33 commits ahead of its
remote and stays unpushed until there is an explicit go-ahead. If a branch appears missing, check
`git worktree list` — one of its worktrees was already found stale and pruned this session.

Her operating limits, condensed: one full evaluation pass plus at most one correction; two
attempts per embedding call; $1.00 generator cap and a separate $1.00 Titan cap; existing
credentials only; no fallback model, region probing, or free-form searches; no retention of
query text, questions, retrieved content, identifiers, credentials or raw provider errors;
stop and ask if any input, checksum, exclusion, model, region, logging, call, cost or
permission boundary changes.

## What's done

Pushed, each verified in isolation on its own branch, each `make prove` PROVEN:

- `6d543cd fix(rag-eval): admit an approved corpus by manifest digest, not by filename case`
- `c94739c feat(rag-eval): let run() ingest a manifest-approved corpus, with a CLI`
- `b72f2a5 feat(origination): admit the approved corpus on the product path, and declare the region`
- `5087b6e fix(rag-eval): enforce the client's operating controls on the provider path` (on the
  sibling branch `fix/rag-eval-titan-controls`)

Both branches also carry `chore(kb): regenerate docs/state.md…` from a **concurrent session**
(`5bdd029` / `82d37f0`) — foreign but harmless. `origin/main` has since advanced to `fb8924f`
(PR #94), and there is a `kb-freshness` gate at `.github/workflows/ci.yml:1065` this session
never ran.

Measured against the real packet: 5 documents admitted, hygiene PASS, audit CLEAN, **66 chunks**
(was 9), 0 chunk ids failing `_CHUNK_ID`, both exclusion fixtures still refused by different
mechanisms (one on content, one on filename).

## What's left

Ordered. Steps 1–3 are ours and need nothing from the client.

1. ~~**Send the correction email.**~~ Sent 2026-08-27; see the status line above. The pre-run email had claimed
   four topics are uncovered and the comparison should be narrowed to the rest. Both halves are
   wrong; see "Blockers" below for the measured numbers.
2. **Map her 28 questions to topic codes and build gold set v2.** `rag_eval/run.py:184`
   `_ALLOWED_GOLD_KEYS` is a strict allowlist of `{id, query, expected, unanswerable, note}` and
   will reject her richer shape. `metrics.py` knows two states; her four outcome classes need a
   third (`clarification` is neither an expected-id case nor unanswerable).
   Expected chunk ids derive from `rag_eval.run.corpus_doc_id(relPath, manifest) + "#" +
   _slug(sourceHeading)`. Under a manifest the doc half is `doc-<approved digest[:12]>`, NOT the
   filename stem: an admitted name is graded by nothing, so it never becomes an officer-visible
   id. The stem form below resolved for all 17 sourced questions and still describes the
   committed slug corpus, but a gold set for the supplied packet must read the doc id from the
   manifest.
3. **Add the conclusion-support assertion** — check the retrieved passage supports her
   `acceptableConclusion` (e.g. "30 days"), not merely that the expected section came back.
   Normalise spelled numbers, or assert on `claimIds` via `policies/policy-manifest.csv`, which is
   robust to rewording.
4. **Re-derive the threshold** against the packet with TF-IDF. `POLICY_RETRIEVAL_MIN_SCORE=0.1609`
   was calibrated for the old 9-chunk corpus and is meaningless now.
5. **Generation pass** (Claude Haiku via Bedrock, her approved one-time offline curation) → **your
   human review of every string** → freeze to versioned config with a digest. The
   `policies/fee_schedule.json` + agreement-test pattern is the house precedent.
6. **The single Titan pass**, two arms (literal topic code vs frozen query), then her required
   report: frozen set + digest, commit SHA, generator and embedding model ids and regions,
   generation and embedding call and retry counts, costs split by generator and Titan, literal vs
   generated results, corpus and exclusion results, support-test results, logging confirmation.
7. **ADR 0007 amendment**, docs-only PR. Rule 6 still describes the naming convention as the
   corpus admission control, which is no longer the whole truth.

## Blockers / open questions

- **The correction email is the immediate blocker on client trust, not on code.** Measured, with
  the literal topic code as query (TF-IDF, k=5, packet corpus): 10 of 17 sourced questions return
  their expected section — Q01, Q03 via `adverse_action`; Q02, Q07, Q27 via `debt_to_income`;
  Q05, Q06 via `apr_finance_charge`; Q10 via `fee_schedule`/`interest_rate`; Q11, Q24 via
  `interest_rate`. **Eight of those ten arrive through the four topics the sent email proposed to
  exclude.** Seven do not retrieve: Q04, Q08, Q09, Q12, Q25, Q26, Q28. Three of five
  `manager_escalation` questions are among the seven.
- **O-3, needs the user:** does acceptance require conclusion accuracy, or is retrieval enough?
  Determines whether step 3 stays small or becomes a new capability.
- **Two client constraints trade against each other.** No retention means no cached vectors, so
  her permitted correction rerun re-embeds the full 66 chunks. Disclosed in the sent email.
- **PR/WIP:** the cap is two and it is breached. Track the live count from the API rather than
  from this file — a written count is false as soon as anything merges.

## Key files

- `rag_eval/run.py:43` `_MANIFEST_DIGEST`, `load_corpus_manifest`, `audit_corpus_against_manifest`
  (`subtree` scopes **both** files and manifest entries), `unsafe_corpus_path_reason`
- `rag_eval/run.py:184` `_ALLOWED_GOLD_KEYS` — the gold-schema blocker for step 2. Widened on
  `feat/rag-eval-gold-set-v2` (cut from `origin/main`, so the line is 71 there): a `--gold` path,
  an `outcome_class` field validated against the four client classes, and a per-class breakdown in
  the report. The mapping itself is still unbuilt.
- `rag_eval/run.py:245` `cache_enabled` / `refuse_traced_provider_run` — both key off the embedder
  instance's `IS_PROVIDER_BACKED`, never off `RAG_EMBEDDER`
- `rag_eval/embedder.py` — `DEFAULT_EMBED_DIMENSIONS`, `_BEDROCK_CLIENT_CONFIG`,
  `EmbeddingProviderError`, call/retry/token counters
- `rag_eval/chunker.py` — `doc = doc_id or path.stem.lower()`, so ids satisfy `_CHUNK_ID`; a
  manifest-admitting caller passes `run.corpus_doc_id`, which derives the id from the approved
  digest rather than the ungraded filename
- `services/origination-service/app/policy_retrieval.py` — manifest admission on the product path,
  fail-closed on every manifest problem
- `services/origination-service/app/config.py` — `POLICY_CORPUS_MANIFEST`
- `services/origination-service/app/policy_retrieval.py::POLICY_TOPICS` — the closed 8-code
  vocabulary the whole mapping question hangs on

## How to verify / run

```bash
python3 -m pytest rag_eval/tests -q                 # 168 on this branch, 165 on the controls branch
./scripts/smoke_rag_eval.sh                          # SMOKE PASS
cd services/origination-service && python3 -m pytest tests -q   # 890 passed, 1 xfailed
docker compose config -q && ./scripts/check_doc_paths.sh
make prove                                           # PROVEN on every commit above
```

Ingesting the packet — **nothing moves into the repo**:

```bash
cp -R <packet>/policies <workdir>/policies
cp <packet>/SHA256SUMS.txt <workdir>/
cd <workdir> && shasum -a 256 -c SHA256SUMS.txt      # 6 of 6 OK
python3 -m rag_eval.run --base <workdir> --manifest <workdir>/SHA256SUMS.txt
```

Her `SHA256SUMS.txt` works **unedited** — `subtree` scoping made that possible. Last dry run:
`gate: 6 files scanned, 0 refused`, 66 chunks, and `hit@1 = 0.00` because the packaged gold set
still points at the old corpus. That zero is expected, not a regression.

**Packet location:** `/private/tmp/claude-501/.../scratchpad/packet` — session scratchpad, will not
survive. The original is `~/Downloads/Mahalakshmi-Meridian-Policy-Client-Inputs-Only-2026-08-25`,
which the sandboxed shell **cannot read** (`Operation not permitted`); copy it out with `!` first.
Never commit it: public fork, and her approval covers indexing, not publishing.

## Branch state

- `main` (`fb8924f`) = the client's real state. Retrieval there runs against our own two
  documents, 9 chunks, lowercase-slug admission only, `POLICY_RETRIEVAL_MIN_SCORE=0.1609`, no
  manifest concept, no provider controls. Cite `git show main:<file>` for client state.
- `fix/policy-corpus-admission` / `fix/rag-eval-titan-controls` = proposed changes, unmerged.

## Debt log refs

- **D16** (pgvector deferred, in-memory exact cosine) — untouched. Still 0 of 3 triggers: 66
  chunks, one caller, no persistence requirement. The Titan switch moves the *cost* argument
  (per-process re-embedding) without moving the *scale* argument.
- ADR 0019 governs policy retrieval; ADR 0007 rule 6 needs the amendment in step 7.
- No new debt entries opened or closed.

## Next session: start here

Send the correction email in `docs/client-asks-2026-08-27-pre-run-disclosures.md`'s "Correction to
send" section (regenerate the wording with the measured numbers above — **eight** of ten, not
seven) — done 2026-08-27. Step 2's schema half is also done on `feat/rag-eval-gold-set-v2`. What
remains is the mapping itself: assign each of the 28 questions to one of the eight topic codes and
emit gold set v2 from `officer-questions-and-acceptance.jsonl`. Note the gold `query` is the topic
code, not her wording — officers select topics — so her question text never enters the file.
