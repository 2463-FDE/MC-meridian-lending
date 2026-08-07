# Regulator watch — 2026-08-07

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

**Coverage gap, stated honestly:** this issue researched live 2026 movement only for the two
active CFPB rules the item named (**Reg B**, **Reg Z**). The other active rows — **FCRA/Reg V**,
**EFTA/Reg E**, **BSA/AML** — are scoped here but **not yet swept** for 2026 changes.
"Active" means *in scope*, not *covered this issue*; an unswept active row carries an owner
and a target below so it cannot read as covered.

**Unswept active rows — owner and target sweep:**

| Active row | Owner (drafts the sweep) | Determination | Target sweep | Follow-up |
|---|---|---|---|---|
| **FCRA / Reg V** (`decision-service` — risk-based-pricing + §615 adverse-action) | Watch-list owner | Priya (Compliance) | **2026-08-14** (next Friday issue) | issue #TBD — swept first, maps to live decisioning code |
| **EFTA / Reg E** (`payment-service` — ACH + card-on-file auth/error-resolution) | Watch-list owner | Priya (Compliance) | **2026-08-14** (next Friday issue) | issue #TBD — swept first, gates the week-5 payment path |
| **BSA/AML + OFAC** (`kyc-service` — CIP, sanctions) | Watch-list owner | Priya (Compliance) | **2026-08-21** (issue after) | issue #TBD — no live 2026 headline yet; event-driven until then |

Open a tracking issue per row before the 2026-08-14 freeze and replace `#TBD` with the
number, so the gap is a ticket a human owns, not a paragraph.

## What moved — as of 2026-08-07

| Date | Regulator / rule | What changed | Meridian relevance | Action |
|---|---|---|---|---|
| **withdrawn 2025-05-12** (released 2023-09-19; 89 FR 27361) | **CFPB Circular 2023-03 — WITHDRAWN** (adverse-action notices, Reg B sample forms) | The circular said a creditor may **not** rely on the checklist of reasons in the CFPB sample adverse-action forms when those reasons do not specifically and accurately state the principal reason(s). CFPB **withdrew** it 2025-05-12 as part of a mass withdrawal of guidance (FR 2025-08286). | **HIGH — and this is a correction to our own work.** The week-3 decision-assistant centrepiece cited Circular 2023-03 as authority. That citation is now stale: the circular is withdrawn. **The underlying obligation is unchanged** — the specificity requirement lives in the statute/regulation itself (ECOA / **12 CFR 1002.9**), not in the circular. | Recite **12 CFR 1002.9** wherever the assistant/docs cited Circular 2023-03; do **not** cite the withdrawn circular. **Stale citations to fix (grep `2023-03`):** `services/decision-service/app/reasons.py:4` (docstring "CFPB Circular 2023-03: no AI exemption"), `docs/spec-decision-assistant-week3.md:7` and `:30`, ADR `adr/0009-decisioning-assistant-design.md`. This is the lead entry on purpose — the client knew of the withdrawal before we did. |
| **2026-04-22** (issued); compliance **2026-07-21** | **Reg B / ECOA final rule** — effects test removed | CFPB removed the "effects test" from Regulation B and states ECOA does **not** recognize disparate-impact liability. Also narrows "discouragement" to statements of *intent* to discriminate, and restricts Special Purpose Credit Programs (bars race/color/national-origin/sex as SPCP common characteristics; extra conditions for for-profit creditors). *(Substance sourced to the CFPB final-rule page. The exact adverse-action **FR-text wording** is **UNVERIFIED** — FR host blocked automated fetch; pending a manual read of FR doc 2026-07804. The liability-theory summary above does not depend on that wording.)* | **HIGH.** We run a credit scorecard (`decision-service`) and issue ECOA/Reg B adverse-action notices. This changes the fair-lending liability posture our scorecard is tested against. | Priya to reassess fair-lending testing under the new standard; confirm adverse-action *notice* mechanics are unchanged (the summary changes liability theory, not the §1002.9 notice content). Add to Monday agenda. |
| **2026-01-01** (effective) | **Reg Z / TILA** — 2026 annual threshold adjustments | Exempt-consumer-credit threshold rose **$71,900 → $73,400** (CPI-W based). Closed-end consumer credit **not** secured by real property/a dwelling **above** the threshold is exempt from TILA. (Also HOEPA/QM/credit-card and escrow asset-size adjustments — mortgage-only, not us.) | **MEDIUM.** Our personal installment loans are unsecured. Any single loan **> $73,400** falls outside TILA, so `disclosure-service` would not be *required* to issue a TILA disclosure for it (we may still choose to). | **Resolved — no action.** Max loan amount is capped at **$50,000** at intake (`amount: float = Field(gt=0, le=50000)`, `services/origination-service/app/schemas.py:54`). $50,000 < $73,400, so no loan can reach the exemption ceiling and every loan stays in TILA scope. Re-check this cap each issue; if it is ever raised above $73,400, reopen this row. |
| **2026-05-01** (issued) | **Reg B / ECOA §1071** — small-business lending rule refocused | Coverage narrowed to core lenders/products/data points; excludes merchant cash advances, small-dollar, and agricultural loans. | **LOW / N/A.** §1071 governs *small-business* lending data collection. Meridian is a *consumer* lender. | None. Track only in case product scope ever adds business-purpose loans. |

## CFPB circulars — status

No **new** Consumer Financial Protection Circular has been issued in 2026. The most recent
circular of record is **2024-07** (Design, Marketing, and Administration of Credit Card
Rewards Programs, 2024-12-30, 89 FR 106277) — not relevant to our unsecured-installment
product.

**Withdrawal that touches us (lead entry above): Circular 2023-03 was withdrawn 2025-05-12.**
It concerned adverse-action notices and the proper use of Reg B sample forms — the exact area
the week-3 decision assistant operates in. Cite **12 CFR 1002.9** (the standing regulation),
never the withdrawn circular. See the *What moved* table for the action.

Re-check the circulars index each week; a new circular — or a withdrawal — is the
highest-signal single event on this page.

## Standing summary for the client

- **Correction we carry:** the week-3 assistant cited **Circular 2023-03**, withdrawn
  **2025-05-12**. The obligation is unchanged — recite **12 CFR 1002.9**, not the circular.
- The one item that touches us this quarter is the **Reg B effects-test removal**, compliance
  **2026-07-21** (already in effect). It changes fair-lending liability theory under ECOA, not
  our disclosure math. Owner: Priya.
- Reg Z's 2026 numbers only reach us at the **$73,400 TILA-exemption ceiling** for large
  unsecured loans — **resolved, no action:** our intake caps loans at **$50,000**
  (`services/origination-service/app/schemas.py:54`), below the ceiling, so every loan stays
  in TILA scope.
- No CFPB circular activity in 2026.

## Recurring mechanism

Free to run, so the enforced thing is the freshness metric, not the tool:

- **By hand (default):** at each Friday freeze, copy this file to next Friday's date and
  refresh the three source pages above. ~15 minutes.
- **Scheduled (optional):** a weekly `/schedule` cloud agent that fetches the three CFPB
  pages + the Federal Register CFPB index and drafts the next dated file for review. A draft
  still needs a human pass before it ships — regulatory claims are not auto-published.

## Sources

- [CFPB — Consumer Financial Protection Circulars](https://www.consumerfinance.gov/compliance/circulars/)
- [CFPB — Circular 2023-03 (adverse-action notices / Reg B sample forms)](https://www.consumerfinance.gov/compliance/circulars/circular-2023-03-adverse-action-notification-requirements-and-the-proper-use-of-the-cfpbs-sample-forms-provided-in-regulation-b/) — withdrawn 2025-05-12
- [Federal Register — Interpretive Rules, Policy Statements, and Advisory Opinions; Withdrawal, 2025-05-12](https://www.federalregister.gov/documents/2025/05/12/2025-08286/interpretive-rules-policy-statements-and-advisory-opinions-withdrawal) (FR doc 2025-08286)
- [CFPB — Equal Credit Opportunity Act (Regulation B) final rule](https://www.consumerfinance.gov/rules-policy/final-rules/equal-credit-opportunity-act-regulation-b/) (compliance date 2026-07-21)
- [Federal Register — Equal Credit Opportunity Act (Regulation B), 2026-04-22](https://www.federalregister.gov/documents/2026/04/22/2026-07804/equal-credit-opportunity-act-regulation-b) (FR doc 2026-07804)
- [CFPB — Small Business Lending under ECOA (Regulation B), 2026](https://www.consumerfinance.gov/rules-policy/final-rules/small-business-lending-under-the-equal-credit-opportunity-act-regulation-b-2026/)
- [CFPB — Truth in Lending (Regulation Z) final rules](https://www.consumerfinance.gov/rules-policy/final-rules/?topics=truth-in-lending-act)
- [Federal Register — Reg Z Annual Threshold Adjustments (Credit Cards, HOEPA, QM), 2025-12-15](https://www.federalregister.gov/documents/2025/12/15/2025-22773/truth-in-lending-regulation-z-annual-threshold-adjustments-credit-cards-hoepa-and-qualified)

## Update log

| Date | By | Change |
|---|---|---|
| 2026-08-07 | initial | First issue. Reg B effects-test removal (HIGH), Reg Z $73,400 exemption (MEDIUM), §1071 refocus (LOW), no 2026 circulars. Reg B Federal Register text verified via CFPB final-rule page — the FR host blocked automated fetch, so the FR-text detail (exact adverse-action wording) is **UNVERIFIED** pending a manual read of FR doc 2026-07804. Added the full **watch universe** (11 rows) — FCRA/Reg V, EFTA/Reg E and BSA/AML scoped as active but not yet swept for 2026 movement. |
| 2026-08-07 | correction | Added **Circular 2023-03 withdrawal (2025-05-12)** as the lead *What moved* entry per mentor feedback — the week-3 assistant cited it; recite 12 CFR 1002.9 instead. Verified via CFPB circular page + FR doc 2025-08286. |
| 2026-08-07 | review | Addressed three review findings. **(1)** Gave each unswept active row (FCRA/Reg V, EFTA/Reg E, BSA/AML) an owner, a determination owner, a target sweep date, and a follow-up ticket slot so "active" no longer reads as "covered". **(2)** Grounded the brownfield claims in repo paths: stale Circular 2023-03 citation at `services/decision-service/app/reasons.py:4` + `docs/spec-decision-assistant-week3.md:7,30` + ADR 0009; max-loan-size at `services/origination-service/app/schemas.py:54` ($50k cap — **resolves the Reg Z row to no-action**); card-storage debt at `docs/debt-log.md` D5/D13 + ADR 0003. **(3)** Reconciled verification status: moved the UNVERIFIED marker onto the exact unverified claim (FR-text adverse-action wording, FR doc 2026-07804) inside the Reg B table row, so the confident liability-theory summary and the log no longer contradict. |
