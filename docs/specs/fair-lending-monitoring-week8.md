# Spec: Fair-Lending Monitoring for the Decisioning Engine (Week 8)

**Status:** draft · **Written:** 2026-08-13 · **Base:** `main` @ `23c1ea1`
**Client ask:** how Lending Ops monitors the decisioning engine for fair-lending risk, answered
by Lending Ops on 2026-08-13 — that answer is the compliance position of record and supersedes
the assumption this spec was going to be written on.
**Companion docs:** the week-8 governance asks and their verbatim answers, on the
`docs/client-asks` and `docs/client-asks-originals` branches (not on this branch);
`docs/cards/week8-governance.md` (the deferrals); the model card (companion deliverable).

---

## Executive Summary

Meridian's decisioning engine produces an outcome and, on an adverse one, up to four Reg B
principal reasons. Nothing today measures whether those outcomes fall differently across
protected groups.

Monitoring cannot be one design, because Meridian's book is not one book. For the
dwelling-secured home-equity line, Meridian collects race, ethnicity, sex and marital status
under Reg B and holds them in the origination system — so disparity is **measured directly**.
For everything else, Reg B generally prohibits collecting those characteristics, so group
membership must be **estimated**, and this spec names the estimation method and states what it
gets wrong rather than presenting an estimate as a measurement.

One rule governs both halves and is the reason the design holds together: **the lending platform
emits, the reporting environment computes.** No protected characteristic and no proxy
probability is stored in this platform. That is Lending Ops' instruction for the collected data
("it lives in the origination system — not in this platform, and I want it kept that way"), and
it is also what preserves the model card's central claim.

Two things this spec does **not** cover, stated here so nobody infers otherwise: **delivery of an
adverse-action notice cannot be proven**, and **pricing disparity cannot be measured**, because
the platform prices every loan identically. Both are detailed under Known Gaps.

---

## 1. Scope

**In scope.** The credit decision: approve, refer and deny outcomes produced by the
`decision-service` scorecard and recorded in `decision_events`, together with the Reg B principal
reasons attached to adverse outcomes.

**Out of scope.** Servicing, collections, payments, and marketing. Pricing is out of scope by
circumstance rather than by choice — see Known Gap 2.

---

## 2. What is measured

Per period, per product, per group:

1. **Outcome rates** — approve, refer and deny as a share of decided applications.
2. **Adverse impact ratio** — each comparison group's approval rate divided by the highest
   group's approval rate. A ratio below 0.80 is a **screen**, not a finding: it opens an inquiry
   and establishes nothing on its own.
3. **Score distribution** — the standardized difference in model score between the highest-rate
   group and each comparison group. Outcome rates can match while the underlying scores do not,
   which matters the moment the bands move.
4. **Principal-reason distribution** — the share of adverse outcomes carrying each reason code
   (R01 delinquency history, R02 excessive obligations, R03 income insufficient, R04 length of
   employment), by group. A group concentrated in one reason code points at a specific model
   driver, which is more actionable than an outcome gap alone.

Each figure is reported with its uncertainty and its denominator. A point estimate without a
count is not reportable under this spec.

---

## 3. Group membership: two paths

### 3.1 Direct measurement — the dwelling-secured book

Meridian originates a small home-equity line. It is dwelling-secured, so Reg B requires the
collection of race, ethnicity, sex and marital status (12 CFR 1002.13), and Meridian collects
them. They are held in the origination system.

For this book, monitoring uses the collected data. No proxy is applied where real data exists —
a proxy would add error for no benefit.

The join happens **in the reporting environment**, on the application identifier. The collected
characteristics are not copied into this platform at any point.

### 3.2 Proxy estimation — everything else

For non-dwelling-secured consumer credit, Reg B generally prohibits collecting these
characteristics, so membership is estimated.

**Method: BISG — Bayesian Improved Surname Geocoding.** It combines a surname-based probability
(from the published Census surname list) with a geography-based probability (from Census
block-group demographics) using Bayes' rule, producing, per applicant, a probability of
membership in each group rather than an assignment to one. Inputs are the applicant's surname
and their address geocoded to block group. It is the method the CFPB uses for the same purpose,
which matters here: an examiner comparing our numbers to theirs is comparing like with like.

**What BISG gets wrong.** Stated, because Lending Ops asked for the error characteristics
stated rather than glossed:

- **Accuracy is uneven across groups.** It is strongest for Black and Hispanic applicants, and
  materially weaker for Asian and Pacific Islander applicants and for groups whose surnames carry
  little distinguishing signal. A single accuracy figure for the method does not exist and should
  not be quoted.
- **Surname coverage is incomplete.** The Census list covers surnames above a frequency floor.
  A rarer surname falls back to geography alone — the weaker of the two signals — and the
  estimate degrades accordingly.
- **Surname signal decays with name change on marriage and with intermarriage**, so the method
  is systematically less reliable for exactly the applicants those apply to.
- **Geographic granularity drives error.** Block group is materially better than ZIP. If the
  reporting environment can only geocode to ZIP, the resulting estimates are weaker and must be
  labelled as such, not silently substituted.
- **Probabilities are used as weights across the portfolio, never thresholded to assign an
  individual applicant a group.** Individual assignment is the standard misuse of this method:
  it discards the uncertainty that is the whole point of the estimate and yields disparity
  figures that do not survive challenge.
- **Measurement error generally attenuates estimated disparity toward zero.** A proxy result
  showing *no* disparity is therefore weaker evidence than a proxy result showing one, and must
  not be reported as a clean bill.

**What follows from all of that:** a proxy finding **opens an inquiry**. It does not establish
disparate impact, and it is never used to take an action on an individual file.

---

## 4. Where the computation happens

**The lending platform emits. The reporting environment computes.**

The platform exports, per period: the decision records (outcome, policy band, model score,
principal reasons) keyed by application identifier, and the application and applicant fields the
reporting environment needs — for the direct path, enough to join; for the proxy path, surname
and address.

The reporting environment performs the join, computes proxy probabilities where they are needed,
and produces the report. **No protected characteristic and no proxy probability is written back
into this platform.**

Two reasons, and the second is the load-bearing one:

1. It is Lending Ops' instruction for the collected data.
2. The model card states that the scorecard reads six inputs and that none of them is a
   prohibited basis — demonstrated, not asserted, by
   `services/decision-service/tests/test_model_vendor.py`. Storing a race proxy inside the
   origination platform would place a race-correlated field within reach of the decisioning path,
   and that claim would then rest on discipline rather than on the data not being there. Keeping
   it out is what keeps the claim demonstrable.

The decision record itself stays identifier-free (`db/init/001_schema.sql:155`, ADR 0007 rule 1).
The earlier concern that monitoring would force identifiers back into decision records does not
arise: the linkage lives outside the platform, so that design survives intact.

---

## 5. Cadence, thresholds, and who acts

- **Data pull:** monthly, so a quarter is never reconstructed at the end of it.
- **Review:** quarterly, by Lending Ops, with Compliance owning the determination.
- **Minimum cell count:** proposed at 30 decided applications per group per period. Below it,
  report the count and withhold the ratio — a ratio computed on single-digit counts moves on one
  decision and invites action on noise. **This figure is a proposal**, because application volume
  by product has not been given to us (Open Item 2).
- **Screen:** an adverse impact ratio below 0.80, or a statistically significant difference in
  outcome rate, opens an inquiry. The inquiry examines the driver distribution (metric 4) before
  anything else, because that is what identifies a model input worth challenging.
- **Retention:** ~25 months, per Reg B and already stated in `policies/underwriting_guidelines.md`.

---

## 6. Known gaps

**Gap 1 — Delivery of an adverse-action notice cannot be proven.**
This platform holds no notice record, no send, no delivery status, and nothing tracking the
30-day clock; `decision_events.decided_at` is a stamp, not a deadline. Letters are produced in
the origination back office from an export, sent by a print-and-mail vendor, with the clock kept
on a spreadsheet. Monitoring can show what was decided and which reasons were recorded. **It
cannot show that a notice reached an applicant, or that it reached them within 30 days.** Carded
as **G1** in `docs/cards/week8-governance.md`, at Lending Ops' instruction, so this spec is not
read as covering it.

**Gap 2 — Pricing disparity is not measurable, because there is no pricing variation.**
Every offer is written at a flat rate: `POLICY_RATE_PCT = 7.99`
(`services/origination-service/app/routers/offers.py:18`), whose own comment records that a
risk-based rate derived from the decision is future work. Meanwhile
`policies/fee_schedule.md:29` describes standard note rates of 7.99%–24.99% by risk band — so
the client's stated product has risk-based pricing that the platform does not implement. Today
there is nothing to measure. **The day banded pricing lands, pricing becomes the second thing
this spec must cover**, and it will not be a small addition: pricing disparity is measured
differently from outcome disparity.

**Gap 3 — Referred applications have no recorded disposition.**
Decisioning is automated end to end and there is no override or manual-approval route anywhere
under `services/`, so a `refer` outcome leaves the platform without a captured final answer.
Monitoring therefore covers approve and deny and cannot follow what happens to a referral — which
is precisely where human discretion, and therefore fair-lending risk, would normally concentrate.
Capturing the disposition is a prerequisite for monitoring it.

**Gap 4 — Cadence and cell counts are proposals, not calibrated figures**, because volume is
unknown to us. See Open Item 2.

---

## 7. Open items for Lending Ops

1. **Name the reporting environment** and who operates it. This spec assumes one exists and can
   reach both the origination system's collected data and this platform's export.
2. **Application volume by product**, so the minimum cell count and the review cadence can be set
   against real numbers rather than proposed.
3. **Name the review owner and the escalation path** — who reads the quarterly report, and what
   happens when the screen trips.
4. **Confirm the geocoding source** available in the reporting environment, and whether it
   resolves to block group or only to ZIP. The answer changes the error characteristics in §3.2
   and must be recorded with the report, not assumed.
