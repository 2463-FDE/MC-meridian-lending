# Client question and answer register

**What this is.** One row per question put to the client, the answer received, and the decision or
artifact it feeds. Four exchanges to date. This is the record meant to live in the repo.

**What this is not.** It carries no email bodies, no paste-ready send copy, and no verbatim reply
blocks. Those exist as working references outside this branch and are deliberately not published —
they are how we arrived at the rows below, not the record itself. Where the client's exact wording
is load-bearing for a decision, a short phrase is quoted; nothing longer.

**Client:** VP Lending Ops, Meridian (referred to below as the client). **Register maintained:**
2026-08-21.

**Relationship to `docs/client-answers-week6-servicing.md`.** This register is a roll-up:
Exchange 1 below condenses the same eleven questions to one line each. The week-6 file stays the
detailed backing record — per-question notes and where ADR 0014 goes further than the answer live
there, and it is what ADR 0014 cites. Update both when a servicing answer changes.

---

## Exchange 1 — servicing money controls

**Asked:** not captured. **Answered:** 2026-08-12, in writing. **Respondent:** VP Lending Ops,
Meridian. **Source reference:** not captured.
Feeds `adr/0014-servicing-money-controls.md` and `docs/cards-week6-servicing.md`.

| # | Question | Answer | Feeds |
|---|---|---|---|
| 1 | Who may move money, and who may read a serviced loan? | CSRs and admins move money; underwriters and borrowers do not. A borrower reads their own account only; underwriters may read any serviced loan. **No supervisor role is to be invented** | ADR 0014 Decision 1 — money roles vs staff roles, reads broader than writes |
| 2 | Are the servicing endpoints reachable from outside the network? | Yes — the borrower portal sits behind the same gateway, so a borrower login is on the public internet | ADR 0014 Context; raised the finding from internal misuse to internet-facing, which is why the authz fix shipped separately from the dashboard |
| 3 | Should a second person approve a discretionary balance move before it lands? | Not this cycle. Record every move now; approval comes next cycle | ADR 0014 Decision 2 and card C1. The approval *design* is engineering's, not the client's |
| 4 | How many discretionary moves happen, and how many people could approve? | ~30 balance adjustments and 15 fee waivers a week, across 9 representatives, with 3 who could approve | The deferral in Q3 rests on this ratio. **Client operating figures, not measured from the database** — nothing in the schema records who adjusted what |
| 5 | What reason must a representative give, and is there a code list? | A reason is required on every manual move. The ops-manual code list was to follow | ADR 0014 Decision 3 — free text plus a permanent `other` escape hatch, so the column is never a closed enum. **List still not received** |
| 6 | What does a controller need to see on an adjustment record? | The figure before and the figure after, on the record itself | ADR 0014 Decision 3 — both stored rather than derived |
| 7 | How long must adjustment history be retained? | **Seven years**, confirmed by the control owner rather than Lending Ops | ADR 0014 Operational impact. **What happens at year eight is undecided and has never been asked** |
| 8 | Should balances be reconciled against payment records before the ledger starts? | No — an honest line in the sand beats a reconstructed history that cannot be defended | ADR 0014 Decision 3 — the ledger opens with one `opening` posting carrying today's stored balance, labelled as such because it may be wrong |
| 9 | Should the borrower be told when a representative adjusts their balance? | No notification for now; record only. Take it together with the payments delivery-channel decision — both need a verified way to reach the borrower, and none exists | Card C2, explicitly not estimable until a channel exists |
| 10 | Is the $150 monthly fee-waiver guideline a limit the system should enforce? | The ops manual sets $150 per account per month with escalation above it. The system has never enforced it and this work does not start | Card C4. **Untested assumption: whether the window is a calendar month or rolling 30 days was never asked** |
| 11 | What is in scope this cycle? | The comprehension work — report, characterization tests, the failing lost-update test, and the decision record. The dashboard waits, and deferrals are to be carded | `docs/cards-week6-servicing.md` C1–C5 exist because of this answer |

## Exchange 2 — decisioning governance

**Asked:** not captured. **Answered:** 2026-08-13, in writing. **Respondent:** VP Lending Ops,
Meridian. **Source reference:** not captured.
Feeds `docs/spec-fair-lending-monitoring-week8.md`, `adr/0016-fair-lending-monitoring-computes-outside-the-platform.md`, `docs/cards-week8-governance.md`, `docs/model-card-decisioning-scorecard.md`.

| # | Question | Answer | Feeds |
|---|---|---|---|
| 1 | Reason truncation: four principal reasons are computed and stored, one is displayed. When should it land? | **Fix this cycle.** Not a display bug — the notice being wrong. Show all of them, up to four, to both applicant and officer | Landed with the governance package |
| 2 | Who owns the adverse-action notice, and does it get all four reasons or the truncated one? | **Not this platform, and it should not become this platform.** Letters are produced in the origination back office: someone exports the decision, a print-and-mail vendor sends it, and the 30-day clock lives on a spreadsheet. It gets whatever the export carries. **"The fix that matters is making the export carry all four, this cycle."** A real notice channel is out of scope — card it, and name it in the monitoring spec as a gap so nobody assumes delivery can be proven | Card G1. **The export half is not verified** — no export endpoint, report or file writer exists under `services/`, and what the back office exports *from* is still an open question |
| 3 | Does Meridian hold applicant race, ethnicity, sex or marital status anywhere, and is any product dwelling-secured? | **Yes to both** — a small home-equity line, for which that data is collected under Reg B and lives in the origination system. **Not to be ingested into this platform.** So monitoring covers both cases: direct measurement for the dwelling-secured book joined in the reporting environment, proxy for everything else with the method named and its error characteristics stated | Replaced our written default of "no such data, all unsecured", which was wrong on both halves |
| 4 | If a licensed vendor model is replacing the deterministic scorecard, its name and version is worth recording | Record it in the model card in those words: a deterministic stand-in, six inputs, none a prohibited basis, **and the test that demonstrates it, cited** | `docs/model-card-decisioning-scorecard.md` |
| 5 | Self-decision authz gap: no check compares caller identity to applicant on the decisioning route. Fix this cycle or card it? | **Do it this cycle, exactly as scoped** — block the decision route when caller and applicant are the same account, leave every other officer action alone. **Log the blocked attempts** (a requirement the ask did not offer) | ADR 0017 and the self-decision authz work |

**Standing constraint from this exchange:** the governance package and the queued balance-correctness
work must not be displaced, and anything bigger than it looks becomes a card and gets reported rather
than absorbed.

## Exchange 3 — settlement reconciliation

**Asked:** not captured. **Answered:** 2026-08-14, in writing. **Respondent:** VP Lending Ops,
Meridian. **Source reference:** not captured.
Feeds `docs/spec-observability-week7.md` (D2, D3, D4), `adr/0015-settlement-reconciliation-as-a-control.md`, `docs/runbook.md`.

| # | Question | Answer | Feeds |
|---|---|---|---|
| 1 | What size of difference does finance consider acceptable to close on? | **$5.00 aggregate per daily close** (500 minor units). Keep it required configuration with no default. Differences above it fail the close and alert; sub-threshold unmatched transactions still appear in the exception output | Spec D4 — the one figure that was missing. Built |
| 2 | Is month-end cut-off the processor-settled day or our-recorded day, and in which timezone? | **Processor-settled date in UTC**, keeping the ±1 calendar-day window so legitimate next-day settlements still match, documented alongside the configuration | Spec D2(c). The UTC half was new work |
| 3 | Is the 12-row, 1–7 June settlement file the full month or a sample? | A seven-day sample, and the complete fixture for this build | Test vectors stay pinned |
| 4 | The month-end gap was described as small; in this extract it is 28% of settled amount. Unrepresentative, or judged against a larger portfolio? | **Treat 28% as material.** "Small" was an inherited assumption, not a measured conclusion. No portfolio-level modelling | ADR 0015 Context |
| 5 | The $500 on loan 4471 — two settled captures with no ledger row. Known and written off, or open? | **An open exception, not a write-off.** Both processor references in the first exception report, marked for Finance Ops review. **"No separate remediation or ticketing workflow is needed in this build"** | Runbook ownership line — Finance Ops named as owner of missing-in-ledger breaks |

## Exchange 4 — payment integrity, card data, monitoring labels

**Asked:** not captured. **Answered:** 2026-08-17, in writing. **Respondent:** VP Lending Ops,
Meridian. **Source reference:** not captured.
Feeds the payment-integrity design, `adr/0013-payment-idempotency-and-tokenization.md`, `docs/spec-payments-week5.md`, `docs/spec-fair-lending-monitoring-week8.md`.

| # | Question | Answer | Feeds |
|---|---|---|---|
| 1 | How long after a payment completes should a repeat submission be treated as the same payment rather than a new one? | **Keep it configurable, keep 24 hours as the working value.** A product choice, not an industry default; a later change is configuration, not rework. **Unprompted addition: forward a separate key to the processor** rather than reusing ours, so the two retention windows cannot cancel each other out | Both halves were already the design. **No spec change needed**, and the vendor's own retention window drops from blocking to informational |
| 2 | Does anything use the second card-charge path? If not, we retire it and enforce a single payment path | **Retirement refused, with a stronger requirement in its place:** do not retire it this cycle; enforce the uniqueness rule in the database so every writer is bound the same way regardless of route; add logging that identifies which handler took a charge; retire later from observed traffic, with a rollback. "A path that still accepts requests is still a live path" | **Surfaced a gap in our design.** The arbiter is a *partial* unique index and the second handler supplies no key, so its rows fall outside the index and are not bound at all. Closing that makes the fix a two-service change |
| 3 | Which processor, and does it offer hosted fields? | **Declined deliberately** — not needed for the fix to proceed; design toward hosted fields or client-side tokenisation | The fix proceeds without tokenisation |
| 4 | Which PCI scope — SAQ-A or SAQ-D? | **Declined deliberately**, same reasoning as the processor question | The fix proceeds without tokenisation |
| 5 | Who owns the purge of stored card numbers and security codes, and by when? | **Left open on purpose** — the purge is a separate effort from the double-charge fix, owner and target date to follow. **Firm on security codes regardless:** collecting them to authorise is fine, keeping them afterwards is not — encrypted or consented makes no difference | The stop-write on security codes becomes its own thin change that waits for nothing — nothing in the platform reads that column |
| 6 | Were the three double-charge customers made whole? | **No answer, deliberately** — "I would rather leave that open than hand you an instruction I cannot stand behind." No customer list, no corrections, and loan 4471 stays an open Finance Ops exception, not a write-off, with no new workflow | Removes work rather than adding it. **Still open on the client's authority** |
| 7 | What is the source of the back-office export — screen, report, or direct database extract? | **Deferred by the client to 2026-08-28** | Would validate the assumption behind exchange 2 question 2 |
| 8 | Are the guidelines and fee schedule current? | **Deferred to 2026-08-28** | Open items summary |
| 9 | What else should be indexed? | **Deferred to 2026-08-28** — asked alongside the currency question, not answered separately | Open items summary |
| 10 | How do updates reach us? | **Deferred to 2026-08-28** — asked alongside the currency question, not answered separately | Open items summary |
| 11 | Which is right where the guidelines/fee schedule disagree with the system? | **Both disagreements answered and confirmed real:** there is no hard cutoff behind the documented debt-to-income rules, and the platform offers a flat rate against a published band. Both are **product direction, not configuration**, so the assistant keeps quoting the documents as written | Confirms current retrieval behaviour. Neither is a defect to fix; both need a recorded home |
| 12 | When a decision returns *Refer*, who makes the final determination and where is it recorded? | **Deferred to 2026-08-28** | Coverage of the monitoring report — referrals leave no captured outcome |
| 13 | Who is the print-and-mail vendor, what does their intake look like, and who owns the spreadsheet timing process? | **Deferred to 2026-08-28** | Card G1's estimate cannot close without all three |

**Also settled in this exchange.** Our four stated monitoring assumptions stand as written, with one
caveat: the per-group volume floor, the geocoding level, and who owns the reporting environment are
**proposals rather than settled facts, and must be labelled that way in the report** so no reader
treats a screen as a finding. And a qualifier that changes document wording rather than facts —
none of the answers is to be treated as a formal compliance determination, which supersedes an
earlier "this is the compliance position" line on status though not on substance.

---

## Open items

**Deferred by the client to 2026-08-28** — asked and waiting, not outstanding on us: the export
source · policy-corpus currency, what else should be indexed, and how updates reach us · who
determines a *Refer* and where it is recorded · the print-and-mail vendor, intake and clock owner.

**Open on the client's authority** — they will return to these: the purge owner and target date ·
whether the three double-charge customers were made whole · the ops-manual reason-code list (**no
record it has arrived**).

**Declined, deliberately** — the answer is the refusal: which processor, and which PCI scope.

**Not yet asked.** Nothing here blocks current work; several would outlive the engagement if never
asked. Who receives the reconciliation alert, at what time, through what channel — *the control fires
correctly and reports to a placeholder address, so it reaches nobody* · when a duplicate charge is
found, is the borrower told, is a refund submitted, who owns it · confirmation that the
duplicate-detection fingerprint excludes the card security code · whether a second identical charge on
the legacy path is ever legitimate · the APR method of record, the applicable tolerance regime, and
whether earlier disclosures need curing · whether the fee-waiver window is a calendar month or rolling
30 days · who runs the daily close and escalates a failed one · what happens to adjustment history at
year eight · who owns each item specified but not built · who accepts the residual risk register · a
control-owner walkthrough of corrected audit-trail statements — no exchange row records this question
having been put to the client.

## Conventions this register follows

- **One question per row.** A row holding two questions loses one when the document is restructured,
  and the loss is invisible afterwards because the row still reads as answered. That happened once and
  cost a control eight days: a two-part row was split, one half was carried and answered, the other
  reached no table at all.
- **Grade an answer's work items against `main`**, not against the branch meant to carry them and not
  against the file a work table predicted. Both mistakes have been made here — one item was found six
  days late, another shipped in a different file than predicted and was nearly recorded as missing.
- **A client answer can constrain a later design.** One reply ruled out a remediation workflow, so a
  later ADR may not propose one. Read the answer before designing.
- **Record what was declined.** A deliberate refusal and an unanswered question look identical in a
  table that only tracks answers, and they call for opposite responses.
