# Regulator watch — 2026-08-14

**A recurring check on Meridian Lending's regulators.** Meridian is a consumer
personal-installment-loan lender, so the regulators that move our obligations are the
**CFPB** (Consumer Financial Protection Bureau) and the rules it administers: **Regulation B**
(ECOA — fair lending and adverse-action notices) and **Regulation Z** (TILA — APR, finance
charge, disclosures). This file records what moved this period, why it matters to us, and
what we do about it — so we can say what changed *before the client tells us*.

## How this file works

- **Cadence:** re-issued weekly at the Friday freeze, before the Monday client demo. Copy
  this file to `docs/regulator-watch-<next-Friday>.md`, refresh the findings, and delete
  nothing — an item that stopped moving becomes a one-line "no change since <date>".
- **Freshness metric (from the weeks 1–4 closure goal):** the newest
  `docs/regulator-watch-*.md` must be **≤ 7 days old at the demo**. Command:
  `ls docs/regulator-watch-*.md | tail -1` vs the demo date.
- **Sources of record:** `consumerfinance.gov/rules-policy/final-rules/`,
  `consumerfinance.gov/compliance/circulars/`, and the Federal Register CFPB index. Every
  row below is dated and linked; a claim we could not verify from a primary source is
  marked **UNVERIFIED** rather than stated as fact.
- **Scope note:** this is a lender-side watch list, not legal advice. Anything with a
  compliance deadline goes to Priya (Compliance Officer) for the actual determination.

## Watch universe — scope

The trainer's item asked for CFPB circulars + Reg B + Reg Z. That is the *minimum*. A
consumer-installment + card lender's real obligation surface is wider, so this is the full
list we watch, each mapped to the Meridian service it governs. **Active** rules get a source
check every issue; **monitor** rules are event-driven (checked when a headline or the client
raises them); **cross-cutting** is a standing enforcement lens, not a rule that "changes".

| Reg (administrator) | Meridian service it touches | Tier |
|---|---|---|
| **Reg Z / TILA** (CFPB) | `disclosure-service` — APR, finance charge, disclosures | active |
| **Reg B / ECOA** (CFPB) | `decision-service` scorecard; adverse-action notices | active |
| **FCRA / Reg V** (FTC + CFPB) | `decision-service` — credit pull, risk-based-pricing + §615 adverse-action | active |
| **EFTA / Reg E** (CFPB) | `payment-service` — ACH + card-on-file recurring, auth + error resolution | active *(pending week-5 payment path)* |
| **BSA/AML + OFAC** (FinCEN / Treasury) | `kyc-service` — CIP, sanctions screening | active |
| **PCI-DSS** (PCI SSC — industry standard, not a govt reg) | `payment-service` — card data storage | active *(current debt: PAN/CVV plaintext — `docs/debt-log.md` D13 (stored, `payments.pan`/`cvv`) + D5 (logged); accepted in `adr/0003-store-card-data-for-convenience.md`)* |
| **MLA + SCRA** (DoD / CFPB) | LOS pricing — 36% MAPR cap, servicemember rate caps | monitor |
| **GLBA privacy + FTC Safeguards** | PII redactor, data-security posture | monitor |
| **FDCPA / Reg F** (CFPB) | `servicing-service` — delinquency/collections | monitor *(only if we collect)* |
| **State usury / licensing** | LOS — APR caps + lending license (TILA discloses, states cap) | monitor *(50-state patchwork)* |
| **UDAAP** (Dodd-Frank §1031/1036) | cross-cutting — every consumer-facing flow | cross-cutting |

**Coverage update:** last issue (2026-08-07) left three active rows unswept for 2026
movement — **FCRA/Reg V**, **EFTA/Reg E**, **BSA/AML**. All three are swept in this issue
(see *What moved* below). Every active row is now covered as of this issue's date.

**Sweep resolution — was "unswept active", now closed no-action:**

| Active row | Determination owner | Swept | Result | Follow-up |
|---|---|---|---|---|
| **FCRA / Reg V** (`decision-service` — risk-based-pricing + §615 adverse-action) | Priya (Compliance) | 2026-08-14 | **No action** — only 2026 CFPB move is the CRA-disclosure max-allowable-charge annual adjustment (binds consumer reporting agencies, not a user/furnisher like us). Does not touch credit-pull, risk-based-pricing notice, or §615 adverse-action content. | issue #TBD — close out; re-open only if a new rule targets furnishers/users |
| **EFTA / Reg E** (`payment-service` — ACH + card-on-file auth/error-resolution) | Priya (Compliance) | 2026-08-14 | **No action** — no 2026 final rule found. Most recent Reg E amendment on CFPB's final-rules page is 2020-06-05 (remittance-transfer disclosure safe-harbor only); a 2025-01-15 CFPB FAQ on the compulsory-use prohibition is a clarification, not a rule change. | issue #TBD — close for this cadence, but **re-check specifically when the week-5 card-on-file recurring-charge feature ships**, not just on the weekly clock |
| **BSA/AML + OFAC** (`kyc-service` — CIP, sanctions) | Priya (Compliance) | 2026-08-21 → **pulled forward, swept 2026-08-14** | **No action** — nothing 2026 touches our CIP/sanctions-screening path. Found: FinCEN RIA/ERA BSA expansion (2026-01-01, doesn't apply — not an RIA), GENIUS Act stablecoin AML/OFAC NPRM (2026-04-08, doesn't apply — no stablecoin), FinCEN whistleblower NPRM (2026-03-30, incentive program, not an obligation change), SAR FAQ clarifications (2025-10, no new requirement). | issue #TBD — close out; event-driven until a live 2026 headline |

Open a tracking issue per row and replace `#TBD` with the number, so the closure is a
ticket a human owns, not a paragraph. **This sweep's determinations are the watch-list
owner's read of primary sources; Priya's sign-off on each row is still the actual
compliance determination** (scope note above) and has not yet been recorded as given.

## What moved — as of 2026-08-14

| Date | Regulator / rule | What changed | Meridian relevance | Action |
|---|---|---|---|---|
| n/a — code fix, not a regulatory change | **Circular 2023-03 stale citation — FIXED** | Last issue's lead entry flagged 4 sites still citing the withdrawn CFPB Circular 2023-03 as authority. All 4 now recite **12 CFR 1002.9** instead: `services/decision-service/app/reasons.py:4`, `docs/spec-decision-assistant-week3.md:7` and `:30`, `adr/0009-decisioning-assistant-design.md` (2 sites). | **Resolved.** The obligation was always in the statute/regulation, not the circular — this closes the correction from last issue. | None further; re-opens only if a new site cites the circular. |
| **2026-08-14 sweep** | **FCRA / Reg V** — swept, no 2026 rule affecting Meridian | See sweep-resolution table above. | **Resolved — no action.** | Row closed for this cadence. |
| **2026-08-14 sweep** | **EFTA / Reg E** — swept, no 2026 rule affecting Meridian | See sweep-resolution table above. | **Resolved — no action**, but gates the week-5 payment path — re-check on ship, not just weekly. | Row closed for this cadence; re-open trigger noted. |
| **2026-08-14 sweep** (pulled forward from planned 2026-08-21) | **BSA/AML + OFAC** — swept, no 2026 rule affecting Meridian | See sweep-resolution table above. | **Resolved — no action.** | Row closed for this cadence. |
| **2026-04-22** (issued); compliance **2026-07-21** | **Reg B / ECOA final rule** — effects test removed | Carried from last issue: CFPB removed the "effects test" from Regulation B; ECOA does **not** recognize disparate-impact liability under the new standard. Narrows "discouragement" to statements of *intent*; restricts Special Purpose Credit Programs. *(FR-text adverse-action wording remains **UNVERIFIED** — FR host still blocks automated fetch; FR doc 2026-07804 still pending a manual read.)* | **HIGH, still open.** We run a credit scorecard (`decision-service`) and issue ECOA/Reg B adverse-action notices. | **Still pending.** No record yet that Priya reassessed fair-lending testing under the new standard, or confirmed adverse-action notice mechanics are unchanged. Carry to Monday agenda again. |
| **2026-01-01** (effective) | **Reg Z / TILA** — 2026 annual threshold adjustments | No change since last issue. | **Resolved — no action** (unchanged). Max loan capped at **$50,000** (`services/origination-service/app/schemas.py:54`), below the **$73,400** TILA-exemption ceiling. | Re-check the cap each issue; no change this issue. |
| **2026-05-01** (issued) | **Reg B / ECOA §1071** — small-business lending rule refocused | No change since last issue. | **LOW / N/A** — governs small-business lending; Meridian is a consumer lender. | None. |

## CFPB circulars — status

No **new** Consumer Financial Protection Circular has been issued in 2026 (unchanged from
last issue). The most recent circular of record remains **2024-07** (Design, Marketing, and
Administration of Credit Card Rewards Programs) — not relevant to our unsecured-installment
product.

Circular 2023-03's withdrawal (2025-05-12) was last issue's lead entry; this issue's lead
entry is that the resulting stale citations are now fixed (see *What moved* above).

Re-check the circulars index each week; a new circular — or a withdrawal — is the
highest-signal single event on this page.

## Standing summary for the client

- **Correction closed:** last issue flagged the week-3 assistant's stale **Circular 2023-03**
  citations (withdrawn 2025-05-12). All 4 sites now recite **12 CFR 1002.9**. Closed.
- **Full watch universe now swept.** FCRA/Reg V, EFTA/Reg E, and BSA/AML+OFAC — the three
  rows carried as "active but not yet swept" since 2026-08-07 — are swept this issue. All
  three resolve **no action** for 2026: no rule change touches Meridian's credit-pull,
  ACH/card-on-file, or CIP/sanctions paths respectively.
- **Still open:** the **Reg B effects-test removal** (compliance date 2026-07-21, already
  in effect) — Priya's fair-lending-testing reassessment and adverse-action-notice-mechanics
  confirmation are not yet recorded as done. Second week carrying this open. Owner: Priya.
- Reg Z's 2026 numbers remain resolved no-action: our intake caps loans at **$50,000**,
  below the **$73,400** TILA-exemption ceiling.
- No CFPB circular activity in 2026.

## Recurring mechanism

Free to run, so the enforced thing is the freshness metric, not the tool:

- **By hand (default):** at each Friday freeze, copy this file to next Friday's date and
  refresh the three source pages above. ~15 minutes when nothing new; longer on a sweep
  issue like this one.
- **Scheduled (optional):** a weekly `/schedule` cloud agent that fetches the three CFPB
  pages + the Federal Register CFPB index and drafts the next dated file for review. A draft
  still needs a human pass before it ships — regulatory claims are not auto-published.

## Sources

- [CFPB — Consumer Financial Protection Circulars](https://www.consumerfinance.gov/compliance/circulars/)
- [CFPB — Equal Credit Opportunity Act (Regulation B) final rule](https://www.consumerfinance.gov/rules-policy/final-rules/equal-credit-opportunity-act-regulation-b/) (compliance date 2026-07-21)
- [Federal Register — Equal Credit Opportunity Act (Regulation B), 2026-04-22](https://www.federalregister.gov/documents/2026/04/22/2026-07804/equal-credit-opportunity-act-regulation-b) (FR doc 2026-07804) — FR-text adverse-action wording still UNVERIFIED, host blocks automated fetch
- [CFPB — Truth in Lending (Regulation Z) final rules](https://www.consumerfinance.gov/rules-policy/final-rules/?topics=truth-in-lending-act)
- [Federal Register — Fair Credit Reporting Act Disclosures, 2025-12-15](https://www.federalregister.gov/documents/2025/12/15/2025-22772/fair-credit-reporting-act-disclosures) (FR doc 2025-22772) — CRA disclosure max-charge adjustment; content UNVERIFIED at FR text level, host blocks automated fetch, summarized from search results
- [CFPB — Electronic Fund Transfers (Regulation E) final rules index](https://www.consumerfinance.gov/rules-policy/final-rules/electronic-fund-transfers-regulation-e/) — most recent amendment 2020-06-05, remittance transfers only
- [FinCEN — RIA/ERA AML program rule](https://www.fincen.gov/) *(effective 2026-01-01; does not apply — Meridian is not an RIA/ERA)*
- [Federal Register — Bank Secrecy Act and Sanctions Compliance Standards for FDIC-Supervised Permitted Payment Stablecoin Issuers, 2026-06-05](https://www.federalregister.gov/documents/2026/06/05/2026-11342/bank-secrecy-act-and-sanctions-compliance-standards-for-fdic-supervised-permitted-payment-stablecoin) *(GENIUS Act NPRM context; does not apply — Meridian issues no stablecoin)*

## Update log

| Date | By | Change |
|---|---|---|
| 2026-08-07 | initial | First issue. Reg B effects-test removal (HIGH), Reg Z $73,400 exemption (MEDIUM), §1071 refocus (LOW), no 2026 circulars. Added the full **watch universe** (11 rows) — FCRA/Reg V, EFTA/Reg E and BSA/AML scoped as active but not yet swept for 2026 movement. |
| 2026-08-07 | correction | Added **Circular 2023-03 withdrawal (2025-05-12)** as the lead *What moved* entry — the week-3 assistant cited it; recite 12 CFR 1002.9 instead. |
| 2026-08-07 | review | Gave each unswept active row an owner, determination owner, target sweep date, and follow-up ticket slot. Grounded brownfield claims in repo paths. Moved UNVERIFIED marker onto the exact unverified claim (FR doc 2026-07804). |
| 2026-08-14 | fix landed | Circular 2023-03 stale citations fixed at all 4 sites (branch `fix/circular-2023-03-citation`) — recited as **12 CFR 1002.9**. Closes last issue's lead correction. |
| 2026-08-14 | sweep | Swept the three carried-forward active rows: **FCRA/Reg V**, **EFTA/Reg E**, **BSA/AML+OFAC** (BSA/AML pulled forward from its planned 2026-08-21 target). All three resolve **no action** for 2026 — see *What moved* and the sweep-resolution table. Reg B effects-test removal carried forward **still open** — Priya's reassessment not yet recorded as done, second week on the agenda. FR doc 2026-07804 and FR doc 2025-22772 both remain UNVERIFIED at the exact-text level (FR host blocks automated fetch); findings above are sourced to CFPB pages and search-result summaries, not a manual FR read. |
