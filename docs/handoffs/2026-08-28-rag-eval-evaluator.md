# Handoff — the evaluator and the graded Titan run (2026-08-28)

> **Where the inputs live.** The policy packet, the displayed-summaries package,
> the gold set and the client-facing correspondence are NOT in this repository and
> must not be added to it: this is a public fork, and the client's approval covers
> indexing her material, not publishing it. Paths to them appear below as
> `<packet>`, `<summaries>` and `<corpus dir>`. This document records the method,
> the decisions and the measured results, which is what a future session needs.


> **SUPERSEDED — do not act on this file. Collapsed to a pointer record on 2026-08-30.**
> The graded Titan pass this file plans **ran on 2026-08-30**. Its status line, its
> six-step "What's left", its blockers, its branch state and its line-numbered file map
> were live claims when written and every one of them is false now. They were removed
> rather than left in place to be read as a runnable plan for a pass that has already been
> spent. The record of what the pass actually did is
> `docs/handoffs/2026-08-30-rag-eval-graded-pass-run-record.md`.

## What this file still records

One fact that survives it and is recorded nowhere else in this repository.

- **Person-name guard trip counts, measured on her 28 rows.** `expected_conclusion_text`
  trips **6** (Q08, Q10, Q12, Q15, Q18, Q24), `prohibitedUnsupportedConclusion` trips
  **1** (Q14), `synthetic_displayed_summary` trips **0**. **None of the seven trips carries
  actual PII** — they are the phrases `Credit Manager`, `Credit Policy`, `Credit Policy
  Schedule` and `If Meridian`. `rag_eval/tests/test_conclusion_fields.py` pins the first
  count; the other two and the zero-PII finding are recorded only here.

Three things this file's earlier banner claimed were unique to it are not. The 30-item
acceptance set — 28 officer questions plus the 2 whole-document exclusion checks — is
`docs/handoffs/2026-08-29-rag-eval-graded-pass.md` § 1, which also names the two check ids
and the `acceptance/` file holding them. That section corrects this file as well as
superseding it: this file recorded both exclusion checks as passing on 2026-08-28, and the
08-29 measurement found `FIX-NEG-BORROWER-FILENAME` admitted (`passed=True`) until
`filename-pii` shipped in #116 (`64f4871`). The `If Meridian`
phrase-replacement defect — the allowlist replaces phrases, so `Meridian Lending` does not
cover a sentence-initial `If Meridian …`, and adding entries will not close it — is carried
by the code it affects, `rag_eval/run.py` (`_NAME_ALLOWLIST`) and
`rag_eval/tests/test_prohibited_conclusion.py`. The two-manifest scoping trap
(`SHA256SUMS.txt` package-relative, `CORPUS-SHA256SUMS.txt` corpus-relative) is
`docs/rag-eval-run-pipeline.md` § "Step 6 — Pick the right manifest".

## Where the live state is

| For | Read |
|---|---|
| What the pass actually did | `docs/handoffs/2026-08-30-rag-eval-graded-pass-run-record.md` |
| The client deliverable | `docs/rag-eval-graded-pass-report-2026-08-30.md` |
| Why the pass was sequenced as it was | `docs/handoffs/2026-08-29-rag-eval-graded-pass.md` |
| Setup (§2), the pipeline (§3), the evaluator's scope (§4) | `docs/rag-eval-run-pipeline.md` |
| The commands, and the failures seen on the day | `docs/runbooks/rag-eval-graded-pass.md` |
| The client record and the S-1…S-10 / C-1…C-7 registers | `docs/handoffs/2026-08-27-rag-eval-support-test.md` |
