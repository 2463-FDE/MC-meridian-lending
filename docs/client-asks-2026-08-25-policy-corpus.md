# Client asks — 2026-08-25 working log, policy corpus + officer questions

**Audience:** Dana (VP Lending Ops, Meridian) · **Sent:** 2026-08-25 · **Answered:** 2026-08-25

**Status: SENT and ANSWERED, with a scoped approval and two return questions.** The reply is
transcribed below. The as-sent email and the verbatim reply still need to land on
`docs/client-asks-originals` under this filename — not yet done.

**Two data flows in the built system are NOT in her description of the approved path.** Her reply
says *"If the implemented data path differs from this description, stop and ask again."* It does.
See "Divergence — must be asked before indexing" below. Nothing gets indexed until that is answered.

**Numbering: rows below are the draft email's own items 1–5.** Item 5 is a consent question, not
an information request, and it is the only item that can block work rather than inform it.

**Relationship to the 08-16 exchange.** Item 6 of the 08-16 email already asked whether the
guidelines and fee schedule are current and *"what else should be indexed"*. She deferred the
currency half to 2026-08-28. This is that deferred half, asked concretely and widened: the 08-16
ask was about the two documents we already have, this one is about the documents we do not.

| # | Ask | Blocks | Dana's response |
|---|-----|--------|---|
| 1 | Which written policy documents can be shared — underwriting manual, credit policy, procedure docs, adverse-action language, exception/override policy, servicing or collections? A partial set is fine | Everything below. The corpus is two files, nine chunks (`policies/fee_schedule.md`, `policies/underwriting_guidelines.md`). At that size we cannot tell a working retrieval system from a lucky one | **Delivered, but synthetic.** Five structured Markdown training policies, synthetic/public, explicitly *"no borrower data or real internal policy"* and *"not legal advice"*. Real client-issued policy is refused for now and required *"before any non-training use"* |
| 2 | Format — markdown or Word, with real headings. PDFs convert badly | `rag_eval/chunker.py::chunk_markdown` splits at headings and has **no size cap and no overlap**, so a long document with no heading structure becomes one chunk that matches everything weakly. Two further failure modes are silent: a repeated heading raises `ValueError` on the duplicate slug and `policy_retrieval._load_corpus` skips **the whole file**, `log.warning` only; and an over-long chunk exceeds Titan's input limit at call time, not at ingest, because `BedrockEmbedder.embed` does no truncation | **Met.** *"structured Markdown"*. Heading structure still needs verifying against the chunker before we trust it — structured is her word, not a guarantee of unique slugs or bounded section length |
| 3 | No borrower information — names, SSNs, account numbers, addresses, dates of birth — in the body **or the filename** | `rag_eval.hygiene.scan_file` refuses a file with findings and `unsafe_corpus_path_reason` refuses an unsafe name. Both are per-file and fail closed to a skip, so one example loan file pasted into an appendix removes that entire manual from the index with nothing surfaced to the officer beyond "no policy match" | **Met, and exceeded.** No borrower data by construction. She also supplied **two isolated whole-document exclusion checks**, which is a test artifact for exactly the silent-skip failure this row describes — we did not ask for those |
| 4 | Fifteen to thirty questions officers actually look up mid-file, in their own words. **Explicitly including** questions policy does not answer, or where the answer is "ask your manager" | `rag_eval/gold_queries.json` holds 12 queries we wrote ourselves against a corpus we authored — it measures our own guesses. Without client-authored questions a larger corpus is unmeasurable: TF-IDF refits idf on every corpus change, so every added document moves every score and `POLICY_RETRIEVAL_MIN_SCORE=0.1609` stops meaning what it meant. The unanswerable class (2 of 12 today) is the only thing proving abstention works — the harness scores those correct when confidence stays *below* threshold. Aim ~1 in 4; a client naturally sends questions their policy answers | **Delivered, 28 questions — and with four outcome classes, not two:** answer, clarification, no-match, manager-escalation. Our gold schema has two states (`expected` ids, or `unanswerable: true`) and `run.py:71` enforces a strict key allowlist, so this does not load as-is. Two of the four classes have no product behaviour behind them at all |
| 5 | **Consent:** we are required to use Amazon's Titan embedding model, so the policy documents are sent once to AWS to be indexed. Borrower data is not involved and the model still never sees the passage. Explicit yes needed before any document is indexed | This is the only item that gates work rather than informing it. A program mandate on the model is **not** client consent for their policy text to leave the environment — different authority. If the answer is no, that is an escalation, not something to resolve on our side | **Approved, narrowly.** Titan indexing/re-indexing of *this supplied packet only*, through *"your existing approved server-side AWS setup"*. Explicitly **not** approved: real internal or client-issued policy, other documents, language-model exposure of policy text, deployment, new credentials, broader AWS use, region probing, fallback models, production use. *"A changed corpus or data flow requires new approval"* |

## What this ask deliberately does not include

- **Any promise about when retrieval improves.** Getting the corpus is a measurement. The
  threshold recalibration, chunker size cap, and k>1 work all follow it and none is scoped here.
- **Borrower-facing documents** (statements, letters, notices). Different PII posture, different
  ask, and not what `search_policy` retrieves.
- **A volume target beyond "roughly ten times".** Three or four real documents is the useful
  threshold — enough that the first problems surface, not so much that ingest becomes its own
  project.

## Draft email — not sent

_The email body for this section is held outside this repository with the other client-facing copies. This log keeps the reasoning, the decisions and her replies; the sent text is not published here._

## Sequence this unblocks

Documents and questions arrive together, then: chunk the corpus so `doc#section` ids exist, map
each client question to its expected ids, re-run `python3 -m rag_eval.run` for a fresh threshold,
and only then consider a retrieval-quality floor in `rag-eval-gate` — which today asserts hygiene,
cache and PII absence but no accuracy floor at all. Adding a floor against the self-authored
12-query set would lock in our own assumptions, so the gold set has to be rebuilt first.

## Her reply, transcribed 2026-08-25

> The attachment contains five synthetic/public structured Markdown training policies, 28 officer
> questions with answer, clarification, no-match, and manager-escalation outcomes, and two isolated
> whole-document exclusion checks. It contains no borrower data or real internal policy, and it is
> not legal advice.
>
> Approved only to index or re-index this supplied synthetic/public training policy corpus with
> Amazon Titan embeddings through your existing approved server-side AWS setup. Policy files leave
> the local environment for AWS during that approved indexing or re-indexing. Not approved for
> borrower data, real internal or client-issued policy, other documents, language-model exposure of
> policy text, live calls by the task that prepared this packet, deployment, new credentials,
> broader AWS use, region probing, fallback models, or production use. If the implemented data path
> differs from this description, stop and ask again. Real current client-issued policy is required
> before any non-training use. This authorization is not a verification that the public repository
> currently enforces this data separation.
>
> No live calls by this task are approved. This approval applies only to the supplied packet. A
> changed corpus or data flow requires new approval. Please replace this synthetic packet with real
> current client-issued policy before any non-training use.
>
> Please confirm the approved corpus boundary and the expected no-match and manager-escalation
> behavior. I will leave the implementation design to you.

## Divergence — must be asked before indexing

Her description of the approved data path is *"Policy files leave the local environment for AWS
during that approved indexing or re-indexing."* Two flows in the built system are not covered by
that sentence, and both send text to Titan:

1. **Every search embeds the query.** `policy_retrieval.search()` calls `embedder.embed(query)` at
   request time, not just at index time. Under `RAG_EMBEDDER=bedrock` that is a Titan call per
   officer search. The query is model-authored text derived from a closed 8-code topic vocabulary
   (`POLICY_TOPICS`), so it carries no borrower data — but it is a live AWS call outside
   "indexing or re-indexing".
2. **Running the eval sends her 28 questions to Titan.** `gold_queries.json` text is embedded to
   score retrieval. Her own packet becomes outbound traffic, and the file's existing privacy
   contract already records this for the Bedrock backend.

Neither is a workaround or a design choice we can make unilaterally — her instruction is explicit.
Both need naming and approving before the first index run.

## The two confirmations she asked for

**1. Approved corpus boundary.** Answerable now, and the mechanism already exists:
`config.POLICY_CORPUS_DIR` (`services/origination-service/app/config.py:420`) points retrieval at
one directory. Setting it to the packet directory makes the indexed corpus **exactly** the approved
packet — the two documents currently in `policies/` are not mixed in, and nothing else can be
picked up. That is a configuration change, not new code, and it makes the boundary literal rather
than a promise.

**2. No-match and manager-escalation behaviour.** No-match is built and is the system's default
failure mode: below-threshold, empty corpus, unset threshold, refused file, unreadable file and
harness-unavailable all return `policy_abstain`, and the officer sees "no policy match". It is
fail-closed by construction, and the two unanswerable gold queries already prove it.
Manager-escalation is **not** a system behaviour — if her corpus says "refer to your manager", that
is an ordinary retrieval hit whose passage happens to say so, and the assistant quotes it. The
distinction matters for her expectations: we can show the passage reaches the officer; we cannot
route, notify or track an escalation, and nothing in the platform does. Clarification is the harder
one — see the implementation plan.
