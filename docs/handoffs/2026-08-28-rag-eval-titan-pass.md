# Handoff — the evaluator, then the graded Titan pass (2026-08-28)

> **Where the inputs live.** The policy packet, the displayed-summaries package,
> the gold set and the client-facing correspondence are NOT in this repository and
> must not be added to it: this is a public fork, and the client's approval covers
> indexing her material, not publishing it. Paths to them appear below as
> `<packet>`, `<summaries>` and `<corpus dir>`. This document records the method,
> the decisions and the measured results, which is what a future session needs.


> **SUPERSEDED — do not act on this file. Collapsed to a pointer record on 2026-08-30.**
> Written before the pass, rebaselined once on 2026-08-29, and overtaken when the pass
> **ran on 2026-08-30**. Its status line ("No Titan call has been made"), its six-step
> "What's left", its blockers, its branch state, its "Next session: start here" and its
> line-numbered file map were live claims and every one of them is false now. They were
> removed rather than left in place to be read as a runnable plan for a pass that has
> already been spent. The record of what the pass actually did is
> `docs/handoffs/2026-08-30-rag-eval-graded-pass-run-record.md`.

## What this file still records

Two facts that survive it and are recorded nowhere else in this repository.

- **O-7 is three topic codes, not four.** `debt_to_income`, `fee_schedule` and
  `interest_rate` carry only unanchored `no_match` questions — they are the client's
  abstention controls, not retirement candidates. `apr_finance_charge` **does** have content
  (Q24), so it is not one of them. `docs/handoffs/2026-08-27-rag-eval-support-test.md` still
  states O-7 as four codes in both its open-question register and its own O-7 entry; this
  correction was never folded back into it. O-6 and O-7 both stay deferred, deliberately.
- **PR #108 merged into a branch, not `main`, because its base was never repointed.**
  It landed in `feat/rag-eval-prohibited-conclusion` (`4471bc4`); PR #109 (`01f8cc1`) then
  carried that branch up to `main`, and that second PR is the only reason the work was not
  stranded. Third instance of this shape: #57, then #45/#46
  (`docs/handoffs/2026-08-21-freeze-scope-response.md`), then this one. Check the base of a
  stacked PR **before** the parent merges, not after.
  `docs/rag-eval-run-pipeline.md` § "F. Build order" records the rescue but not the cause.

Everything else this file carried is now held where it belongs. The `us.` prefix being a
cross-region inference profile rather than a region pin, and the `AWS_BEARER_TOKEN_BEDROCK`
resolution needing no code change, are in `docs/rag-eval-run-pipeline.md` § 2 and
`docs/runbook-rag-eval-graded-pass.md`. The polarity-inversion caveat on a shared prompt, the
measured prohibited-axis cost, and the load-bearing `PROHIBITED_STATES` ordering are in
`docs/rag-eval-run-pipeline.md` § 4. The D16 retention question — persisting Titan vectors
needs an ADR 0007 rule 6 amendment and a PII re-review, because her authorization covers
indexing and not retention — is in `docs/debt-log.md` under D16.

One piece of housekeeping this file named is still undone: `feat/rag-eval-conclusion-fields`
and `feat/rag-eval-prohibited-conclusion` are both merged and both still exist on `origin`.

## Where the live state is

| For | Read |
|---|---|
| What the pass actually did | `docs/handoffs/2026-08-30-rag-eval-graded-pass-run-record.md` |
| The client deliverable | `docs/rag-eval-graded-pass-report-2026-08-30.md` |
| Why the pass was sequenced as it was | `docs/handoffs/2026-08-29-rag-eval-graded-pass.md` |
| Setup (§2), the pipeline (§3), the evaluator's scope (§4) | `docs/rag-eval-run-pipeline.md` |
| The commands, and the failures seen on the day | `docs/runbook-rag-eval-graded-pass.md` |
| The client record and the S-1…S-10 / C-1…C-7 registers | `docs/handoffs/2026-08-27-rag-eval-support-test.md` |
