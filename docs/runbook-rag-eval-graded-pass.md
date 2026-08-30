# Runbook — the graded RAG eval pass

> **Where the inputs live.** The policy packet, the displayed-summaries package,
> the gold set and the client-facing correspondence are NOT in this repository and
> must not be added to it: this is a public fork, and the client's approval covers
> indexing her material, not publishing it. Paths to them appear below as
> `<packet>`, `<summaries>` and `<corpus dir>`. This document records the method,
> the decisions and the measured results, which is what a future session needs.



## STATUS — all four phases executed 2026-08-30. The pass is spent.

| Phase | When | Outcome |
|---|---|---|
| A — fake-judge rehearsal | 2026-08-29, re-run 2026-08-30 post-#116 | PASS: 28 graded, 8 topics, packet 18 OK |
| B — one real Bedrock call | 2026-08-30 | FAILED first (see below), PASS after PR #119 |
| C — the graded pass | 2026-08-30 | Complete: 94 embed + 28 judge calls, 0 retries |
| D — cleanup | 2026-08-30 | 18 OK, no `rag_eval` residue in the packet |

Results: `docs/handoffs/2026-08-30-rag-eval-graded-pass-run-record.md`.
Deliverable: `docs/rag-eval-graded-pass-report-2026-08-30.md`.

**S-7 is now exhausted for this gold set.** Every one of the 28 cases has been
graded once. Re-running any of them is a second sample and is not permitted without
going back to the client. This runbook stays for a future pass on a NEW gold set or
a changed corpus.

## The one rule

**S-7 allows a single bounded pass.** One retry for a *technical* failure only. A
reply that arrives and is wrong is a result, never retried. No prompt tuning against
results you have seen. No model fallback.

Everything below exists so the pass is spent once, on inputs that are ready.

## What is in this directory

| File | Purpose |
|------|---------|
| `gold_v2.json` | 28 gold rows. Four carry a `support_literal`. |
| `<packet>/` | Her frozen policy corpus. 18 checksummed files. |
| `<summaries>/` | Her frozen displayed summaries. Separate manifest. |
| `rehearse_fake_judge.py` | Phase A. Full 28 rows, fake judge, no provider. |
| `smoke_bedrock_judge.py` | Phase B. One real Bedrock call, synthetic case. |
| `rehearsal-report-<date>.md` | Phase A output. **Not a deliverable.** |

`<repo root>` below is the checkout holding `rag_eval/`.

---

## Phase A — rehearsal, free

```bash
cd <corpus dir>
PYTHONPATH=<repo root> python3 rehearse_fake_judge.py
```

Runs all 28 rows through `run()` with a deterministic stand-in. No socket is opened.

Expected:

```
cases graded : 28 (expected 28)
topics       : 8 (expected 8)
packet       : 18 OK (expected 18)
PHASE A PASS
```

Anything else stops here. The script deletes the `rag_eval/` copy `run()` leaves
inside the packet and re-verifies the checksums itself.

**The report it writes is not a deliverable**, and carries a banner saying so.
`run.py` derives the judge backend from "an evaluator exists" rather than from what
was configured, so a fake run's report names Bedrock for a run that never called it.

Do not make the fake periodic. The first version cycled verdicts on a 4-counter, and
every `records_retention` and `apr_finance_charge` row sits at index 3 mod 4 — all of
them landed on `human_review` and two topics reported `n/a (0 graded)`. A periodic
fake manufactures the exact failure this phase exists to detect. It now keys off a
hash of the query id.

---

## Phase B — contract smoke, one real call

```bash
cd <corpus dir>
export AWS_REGION=us-east-1
export AWS_BEARER_TOKEN_BEDROCK=...        # env var only, never a literal
PYTHONPATH=<repo root> python3 smoke_bedrock_judge.py
```

Proves, on an **invented** case (a widget warranty — nothing resembling her corpus):

- the credential resolves
- model access exists in the region for the configured profile
- the Bedrock envelope parses
- the reply is the fixed four-key JSON
- verdicts are in-vocabulary, including the prohibited axis's inverted polarity
  (`avoided` / `asserted`, never `supported` / `unsupported`)

Expected: `PHASE B PASS -- contract holds.`

**Judge the contract, not the answer.** The case is synthetic; the verdict carries no
information about quality. If it looks wrong, that is not licence to edit the prompt —
that is the prompt tuning S-7 forbids. If it *fails*, fix the defect it names, then
re-run: a synthetic case is not one of her 28, so re-running spends nothing she owns.

Refuses without an explicit `AWS_REGION`, without a credential, or with
`LANGSMITH_TRACING` set. Exit 2 means it refused before calling; exit 1 means it called
and the contract failed.

Skipping this phase means the first real Bedrock contact is the pass that cannot be
repeated.

---

## Phase C — the graded pass

Preconditions, each of which fails the run rather than degrading it:

```bash
export RAG_EMBEDDER=bedrock
export RAG_JUDGE=bedrock
export AWS_REGION=us-east-1
unset LANGSMITH_TRACING          # BedrockEvaluator refuses to construct otherwise
# LLM_TRACE_CONTENT must be false
```

```bash
PYTHONPATH=<repo root> python3 -m rag_eval.run \
  --base <packet> \
  --gold gold_v2.json \
  --manifest <packet>/SHA256SUMS.txt \
  --displayed-summaries-manifest <summaries>/SHA256SUMS.txt
```

Pass **both** manifests. Passing only `--manifest` leaves the summaries audit
unexercised — a non-Titan failure mode, skipped by the step meant to catch those.

Record from the run's own output, before doing anything else:

- the calibrated threshold
- the embedder signature and the corpus signature
- provider calls, retries, input-token total
- the judge call count

The threshold **recalibrates**. It is bound to one (corpus, embedder) pair, and Titan
moves the embedder side, so the TF-IDF value does not carry over.

Approved ids: generator `us.anthropic.claude-haiku-4-5-20251001-v1:0`, embedding
`amazon.titan-embed-text-v2:0` at dimension 1024, both `us-east-1`. The `us.` prefix
is a cross-region inference profile — `us-east-1` is accurate as the **calling**
region and would be overstated as a residency claim.

---

## Phase D — clean up, every single time

```bash
rm -rf <packet>/rag_eval
cd <packet> && shasum -a 256 -c SHA256SUMS.txt   # expect 18 OK
```

Never commit the packet, the summaries package, the gold set, or a report. Public
fork; her approval covers indexing, not publishing.

---

## Reading the result

- Report **by topic, never pooled** (S-4). All eight topics stay in scope.
- `human_review` counts in neither numerator nor denominator (S-9). Its size says
  something about the evaluator as well as about the corpus. Empty it by hand.
- Four topics hold exactly one case — `debt_to_income`, `fee_schedule`,
  `interest_rate`, `apr_finance_charge`. A single `human_review` there erases the
  topic from the deliverable, and there is no second pass. State that in the report
  rather than shipping a blank row.
- State the S-6 reading explicitly: the conclusion axis only, so the four
  literal-backed rows still reach the model on summary and prohibited. It is not
  confirmed by the client, and ADR 0022 records it as an open risk.

## If something fails mid-pass

A provider failure raises after one retry, leaving a partial set. S-7 does not permit
re-running the remainder as though nothing happened. Stop, record what was spent
(`calls`, `retries` from the run output), and take the question back to the client
before spending anything further.

### Failures actually seen on 2026-08-30, and what each looked like

All three were caught BEFORE the graded pass, by Phase B and by the guards. None
spent a gold case.

**1. `ModuleNotFoundError: No module named 'rag_eval'`.** `PYTHONPATH=~/...` passed
as an argument to `env` — zsh does not expand a tilde there (`MAGIC_EQUAL_SUBST` is
off by default), so the path stayed the literal string `~/...`. Use `$HOME`. Costs
nothing; it dies at import.

**2. `AccessDeniedException: Invalid API Key format: Must start with pre-defined
prefix`, on every model including Titan.** Not a model-access problem: the
credential itself was malformed. The shell variable held the whole assignment line
(`AWS_BEARER_TOKEN_BEDROCK=ABSK…`) because that line was pasted into `read -rs`,
which takes the line literally. Diagnosable without spending anything: a long-term
key is `ABSK` + base64, so the length minus 4 must divide by 4. It was 157 (= 25 +
132). Check the prefix against the long-term-key prefix that the `secret-scan`
job pins in `.github/workflows/ci.yml` — this file is scanned by that job, so the
literal cannot be repeated here — and that the body length divides by 4 before
spending a call.

**3. The model fences its JSON.** Phase B returned `reply text is not JSON`, with
the reply starting ` ```json `. `grade()` called a bare `json.loads`, which raised,
and the except branch returned all three axes at `human_review` — which on the pass
would have fired on all 28 cases and produced a report with no rates in it, with no
rerun available. Fixed in PR #119 (`_first_json_object`), merged before the pass.

**This is why Phase B is not optional.** Not one of these three would have been
visible to a fake, and the third would have destroyed the deliverable silently.
