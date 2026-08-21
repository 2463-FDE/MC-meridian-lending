# ADR 0018: Policy Retrieval on the Assistant Loop — the Model Chooses the Lookup, Code Renders the Citation

- **Status:** **Accepted** — built in this PR (`search_policy` tool, `app/policy_retrieval.py`).
- **Date:** 2026-08-15
- **Author:** Claude Code
- **Related:** ADR 0005 (LLM client — the least-privilege export contract this decision works
  within), ADR 0006 (logging redaction), ADR 0007 (RAG corpus hygiene — what may enter a
  retrieval corpus), ADR 0009 (decisioning assistant, §5 — the loop this tool joins), ADR 0016
  (fair-lending monitoring computes outside the platform). Card G2a/G2b,
  `docs/cards-week8-governance.md`. Debt D16 (`docs/debt-log.md`) — pgvector stays deferred.
- **Source:** card G2b, and the redaction contract read at `app/llm/request_builder.py`.

---

## Context

Loan officers ask policy questions — what the late fee is, how the payment waterfall works,
what the note-rate bands are. The platform answers none of them today. `rag_eval/` measures
retrieval quality offline and no service imports it; the assistant loop
(`POST/GET /assistant/decisions/{app_id}`) carries two tools, `score_application` and
`get_decision_record`, both of which answer questions about one application, not about policy.
Officers read `policies/fee_schedule.md` and `policies/underwriting_guidelines.md` themselves,
or ask a colleague.

Three facts constrain any answer to this.

**The corpus is committed, curated, and gate-checked.** `policies/` holds the two documents
above; the hygiene gate in ADR 0007 refuses any corpus file carrying PII and blocks the build
(`rag-eval-gate`). The corpus is 9 chunks, and TF-IDF scores hit@3 = 1.00 on the gold
queries. It is bind-mounted read-only into origination and, since card G2a, the `rag_eval`
package is importable in the container.

**Free text cannot cross the LLM boundary.** The export contract fails closed:
`_redacted_turn` requires every history turn's content to be a JSON object, and
`_redact_scalar` masks any whitespace-bearing string wholesale
(`app/llm/request_builder.py:236-238`) and any single token that matches no known shape
(`:260-264`). Only values in the `_SAFE_CATEGORICAL` allowlist survive (`:59-78`). This is
deliberate: a lowercased bare name is byte-identical to an operational code, so the contract
never passes a string on shape alone. A retrieved policy paragraph is whitespace-bearing free
text, so under today's contract the model receives `•••• (free text redacted)`.

**Generation must not sit on the causal path of a regulated decision.** Reg B principal
reasons come from `decision-service/app/reasons.py` and are deterministic; ADR 0009 §5 already
requires the assistant's final answer to be validated against the persisted record, and
`_constructed_summary` builds the officer-facing text from that record rather than from the
model's narration. The corpus also contains `underwriting_guidelines#adverse-action-reg-b`,
so retrieval that runs during a decision could feed adverse-action wording into the path that
produces one.

There is also a measured failure mode. `rag_eval/eval_report.md` records the query "Why was
application #6012 denied?" retrieving the Reg B chunk at 0.338, above the calibrated
threshold, and marks it **false-confident**: an application question answered with a policy
paragraph. Retrieval that cannot route such a question elsewhere will answer it wrongly and
with confidence.

---

## Decision

We will add `search_policy` as a third tool on the assistant loop. The model chooses what to
look up; code decides what is true and what the officer reads.

1. **The model supplies a free-text query.** It is used in-process for retrieval only and is
   never echoed back into the model's history, so the query never returns across the boundary.
2. **The tool result the model sees carries codes and numbers only** — `status` of
   `policy_hit` or `policy_abstain`, plus the match score. Both status values are added to
   `_SAFE_CATEGORICAL` as compound operational tokens. The export contract is unchanged: no
   exemption, no new channel, no free text.
3. **The officer reads the corpus text verbatim, rendered by code**, with its chunk id as the
   citation, appended to the assistant's record-derived summary. The model never sees the
   policy text, so it cannot paraphrase, compress, or contradict it.
4. **Retrieval abstains below a configured score threshold**
   (`POLICY_RETRIEVAL_MIN_SCORE`), which has no committed default. Unset means the tool
   abstains on every query — a fail-closed posture, but not the same one as the origination fee
   schedule: origination decisions, boards and discloses without retrieval, so an unset
   threshold is a disabled feature rather than an unhealthy service, and `/health` does not
   report it (`config.py::missing_required_secrets`). `policy_retrieval` logs the reason once
   per process instead.
5. **Retrieval is refused entirely on `task="decision"`.** The tool is available on the
   read-only `explain` task. A decision run that requests it receives an explicit refusal
   code, so policy prose can never enter the turn sequence that produces a regulated outcome.
6. **The corpus is re-verified at load.** `policy_retrieval` runs `rag_eval.hygiene.scan_file`
   over every corpus file it indexes and refuses a contaminated or unreadable file rather than
   indexing it, so a wrongly-pointed mount cannot put applicant data into an officer's screen.

### Options considered

**Option A — code renders the citation (chosen).** The model orchestrates, code retrieves and
quotes. Rejected reasons do not apply; the trade-off is that the model cannot reason over the
policy text, so an answer is an excerpt plus a citation rather than a synthesis across
documents.

**Option B — exempt corpus text from the redaction contract.** Pass a string raw when it is
byte-identical to a chunk in the gate-passed corpus. This is a real design: the membership
test is self-verifying, and applicant data cannot match a committed corpus chunk. We reject
it for this change because it modifies a security control to add a product feature. The
control's value comes from having exactly one rule — no free text leaves — and each exemption
costs a review of every path that could reach it, its own blocking gate, and a fresh
adversarial pass. Option A delivers the officer-facing capability without that cost. If
officers report that excerpts are insufficient, Option B is the upgrade path and gets its own
ADR.

**Option C — a standalone `/policy/ask` route.** Simplest to build and independent of the
assistant. We reject it on measured evidence: the false-confident #6012 case above shows a
bare policy route answering an application question with a policy paragraph. On the loop, the
same question routes to `get_decision_record` instead, because the model has both tools and
the task field tells it which one the officer asked for.

**Option D — defer retrieval to next cycle.** Rejected because `docs/plan-weeks7-10.md` §2
makes retrieval working end to end on `main` the bar for the W10 handover, and the seam is
already built, so the remaining work no longer competes with the governance package or the
balance-correctness work.

---

## Consequences

### Positive

- Officers get an answer with a verbatim quotation and a citation to the source document —
  auditable, and traceable to a committed file rather than to model output.
- The LLM export contract, the Reg B causal path, and the deterministic reason codes are all
  untouched. This change adds no new class of data crossing the trust boundary.
- Policy text costs no provider tokens, because it never enters a prompt.
- Retrieval is exercised end to end on `main`, which is what the W10 handover claim requires.

### Negative / trade-off (accepted)

- The model cannot synthesize across policy documents, compare clauses, or apply a rule to an
  application's facts. It picks a query; the officer reads the excerpt and applies judgment.
- Excerpt quality is chunk quality. A question whose answer spans two chunks returns the
  better-scoring one, and the officer follows the citation for the rest.
- The threshold is a configured constant per deployment, and a wrong value degrades quietly in
  one direction: too high abstains on good matches. `rag_eval` remains the instrument for
  calibrating it, and the report records the calibrated value per corpus.
- One more environment variable to set, and `/health` reports unhealthy until it is.

### Neutral

- The index stays in memory and is rebuilt per process (debt D16, ADR 0007 rule 6). Nine
  chunks and exact cosine make this microseconds; pgvector stays gated on corpus growth.
- The embedder is whichever `RAG_EMBEDDER` selects, TF-IDF by default. Bedrock works
  unchanged, and this decision does not depend on which is chosen.

---

## Cross-cutting concerns

- **Security.** No new data leaves the trust boundary: the query stays in-process, the tool
  result is allowlisted codes, the excerpt goes to the officer's browser, not the provider.
  The hygiene scan at load is the compensating control for the corpus arriving over a mount.
- **Performance.** Index build is one pass over 9 chunks at first use, cached per process;
  search is exact cosine over that set. Neither is measurable next to the provider call the
  loop already makes.
- **Scalability.** Linear search degrades with corpus size; D16 names the trigger and the
  successor (`PgVectorIndex` behind the existing `Index` contract).
- **Reliability.** Every failure fails closed to abstain: missing corpus directory,
  contaminated file, unreadable file, unset threshold, or a score below it. The assistant run
  continues and the officer sees an honest "no policy match", never a fabricated one.
- **Maintainability.** Retrieval lives in one module (`app/policy_retrieval.py`) and the loop
  calls it. Adding a corpus document requires no code change; it does require re-running
  `rag_eval` to confirm the threshold still separates hits from misses.
- **Cost.** Zero additional provider spend. The excerpt never becomes prompt tokens.
- **Operational impact.** One new environment variable, reported by `/health` when unset. The
  corpus mount already exists in `docker-compose.yml`.
- **Testing impact.** Unit tests cover abstain-below-threshold, refusal on `task="decision"`,
  the query never entering history, hygiene refusal of a contaminated corpus file, and the
  officer-facing excerpt being byte-identical to the corpus chunk.

---

## Implementation plan

1. `app/policy_retrieval.py` — load and hygiene-scan the corpus, chunk it with
   `rag_eval.chunker`, embed with the selected `rag_eval` embedder, index with
   `rag_eval.index.InMemoryIndex`, and expose one `search(query)` returning a hit with the
   verbatim chunk text and its id, or an abstention.
2. `app/config.py` — `POLICY_RETRIEVAL_MIN_SCORE` (no default) and the corpus directory;
   report the missing threshold from `missing_required_secrets()`.
3. `app/assistant.py` — `_search_policy` tool, third entry in `_TOOLS`, dispatch that passes
   the model's query rather than the application id, the `task="decision"` refusal, and
   citation collection into the officer-facing result.
4. `app/prompts/decision_assistant.py` — declare the tool, extend the output schema with the
   tool name and a `query` input, and state the two rules the model must follow.
5. `app/llm/request_builder.py` — add `search_policy` to the `tool` vocabulary and the two
   `policy_*` status codes.
6. Tests, and `scripts/spec_gate_map.txt` pairing this ADR with the retrieval module.

## Rollback strategy

Remove the `search_policy` entry from `_TOOLS`, the tool name from the prompt's output schema,
and the tool paragraph from the system prompt. The loop then rejects the tool by name through
its existing unknown-tool path, and no other behavior changes. Leaving
`POLICY_RETRIEVAL_MIN_SCORE` unset is the operational kill switch and needs no deploy: every
query abstains. Nothing is persisted by this feature, so there is no data to migrate back.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| A policy question is answered from a stale document — both corpus files are marked *last reviewed 2024-11* | Every answer carries its citation, so the officer sees which document and section it came from. Currency of the documents is a client-owned question, raised in the outstanding ask to Lending Ops. |
| The corpus states a rule the platform does not implement — the guidelines set DTI cutoffs at 43%/50% and no DTI threshold exists in code (`decision-service/app/model_vendor.py:40-41` uses DTI as a scoring term) | Named in the same client ask. Until it is answered, this is a documented divergence between a published policy and system behavior, not a retrieval defect — retrieval quotes the document accurately. |
| An application question retrieves a policy chunk (the measured #6012 case) | The tool sits on a loop that also holds `get_decision_record`, and the task field tells the model which the officer asked for. The threshold abstains on weak matches. |
| A wrongly-pointed corpus mount exposes applicant data | The hygiene scan runs at load and refuses the file; the offline gate (`rag-eval-gate`) covers the committed corpus. |
| Threshold drift as the corpus grows | `rag_eval` recalibrates and reports the value; adding a document is a prompt to re-run it. |

## Assumptions challenged

- *"Retrieval means the model reads the documents."* It does not have to. The officer is the
  reader; the model is the router. Once stated that way, the redaction contract stops being an
  obstacle and becomes a boundary the design sits inside.
- *"The card's 0.5–1 day estimate holds."* It does not. It assumed the loop's tool dispatch
  needed no change, but dispatch passes only the application id to every tool
  (`app/assistant.py:243-244`), and it assumed tool results could carry prose, which the
  export contract refuses.
- *"The corpus needs to ship in the image."* It does not. `docker-compose.yml` has mounted
  `./policies` read-only into origination since the initial scaffold.

## Sign-off status

Open, and not blocking this implementation: whether the 2024-11 corpus is current, who owns
its updates, and whether the published DTI cutoffs or the implemented behavior is correct.
All three sit in the outstanding ask to Lending Ops (email 2).
