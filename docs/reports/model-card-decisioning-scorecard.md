# Model Card — Meridian decisioning scorecard

**Model:** `meridian-risk-stub` v1 (`model_signature()` → `meridian-risk-stub:v1`) ·
**Status:** in production on this platform · **Card written:** 2026-08-13 ·
**Base:** `main` @ `23c1ea1`
**Companion docs:** `docs/specs/fair-lending-monitoring-week8.md` (how outcomes are monitored),
ADR 0009 (the decisioning design this implements), ADR 0011 (KYC before decisioning)

---

## The sentence an examiner reads first

**The scorecard is a deterministic stand-in for a licensed vendor risk model. It reads six
inputs. None of them is a prohibited basis, and no identity field reaches it. That is
demonstrated by test, not asserted:** `services/decision-service/tests/test_model_vendor.py` —
`test_scorecard_reads_exactly_the_six_declared_inputs`,
`test_scorecard_reads_no_prohibited_basis_even_when_handed_one`, and
`test_prohibited_basis_cannot_change_the_score_or_the_reasons` — plus
`services/decision-service/tests/test_decision.py` —
`test_run_decision_never_hands_ssn_fingerprint_to_score_application`.

Those tests do not restate the claim. The first three record every key the model actually reads
while scoring on a synthetic input dict, so a future change that starts reading a seventh key
fails even when the score is unchanged; the third passes every Reg B prohibited basis (12 CFR
1002.2(z)) and every direct identifier an applicant record carries into the model deliberately,
and requires the score and the attributions to come back identical — on the denial path too,
where the attributions become the reasons an applicant is told. **The fourth closes the gap the
first three leave: it drives the real production path** (`decision._run_decision()`), where the
`ssn_fingerprint` `decide()` adds for idempotency-conflict detection lives in the same dict that
gets persisted — and asserts the keys actually handed to `score_application()` are the six
declared inputs and nothing else. `_run_decision()` calls `score_application()` before
`ssn_fingerprint` is ever merged into that dict, so a licensed model plugged in behind
`score_application()` cannot receive an identity-derived field just because it shares a payload
with the persisted/replay metadata.

---

## 1. Model details

| | |
|---|---|
| Identity | `MODEL_ID = "meridian-risk-stub"`, `MODEL_VERSION = "1"` |
| Type | Deterministic rule-based scorecard. Not statistical, not trained, not learned |
| Owner | Engineering, on behalf of Lending Ops |
| Location | `services/decision-service/app/model_vendor.py` |
| Determinism | Same input, same output. No randomness, no clock, no network |

**This is a stand-in, and the card says so on its first page at the client's instruction.** No
licensed model artifact exists. The module emits the *shape* a real vendor model would — a score,
a model identity, and ranked signed feature attributions — so the integration around it is real
even though the model is not. A licensed model replaces this module behind `score_application()`
without touching the write path.

**When that happens, this card is rewritten against the replacement**, named and versioned. A
model card describes whatever is actually deciding; it does not survive the model it describes.

## 2. Intended use

Producing a credit decision — approve, refer, or deny — on a consumer personal-installment loan
application, together with the specific principal reasons for an adverse outcome.

**Not intended for:** pricing (the platform prices every loan identically, see §6), servicing or
collections decisions, marketing or pre-screening, or any use on the dwelling-secured home-equity
line, which this platform does not originate.

## 3. Inputs

Six, and only six:

| Input | Source |
|---|---|
| `bureau_score` | credit bureau |
| `annual_income` | application |
| `requested_amount` | application |
| `term_months` | application |
| `monthly_debt` | application |
| `employment_years` | application |

**Not inputs, and demonstrably not read:** race, color, religion, national origin, sex, marital
status, age, receipt of public assistance, exercise of rights under the CCPA — every prohibited
basis under 12 CFR 1002.2(z) — and the direct identifiers the applicant record holds (name, date
of birth, SSN, address, ZIP).

The platform holds no protected-characteristic data at all for this book, which is the expected
Reg B posture for non-dwelling-secured consumer credit rather than an oversight. The decision
record itself is identifier-free by design (`db/init/001_schema.sql:155`, ADR 0007 rule 1).

## 4. How the score is produced

A base score of 640 is adjusted by four signed feature contributions:

| Feature | Derived from |
|---|---|
| `delinquency_history` | bureau score relative to base |
| `payment_burden` | debt-to-income including the new payment |
| `income_sufficiency` | income relative to requested amount |
| `employment_tenure` | years employed, capped at 10 |

Attributions are returned ranked most-negative first — the ordering the adverse-action reason
mapping consumes.

## 5. Decision thresholds and reasons

**Bands**, sourced from `policies/underwriting_guidelines.md`, not invented in code:
approve at 660 and above, refer from 600 to 659, deny below 600.

**Reasons** are mapped from the model's real negative attributions, never generated and never a
generic fallback (`services/decision-service/app/reasons.py`):

| Feature | Code | Reason |
|---|---|---|
| `delinquency_history` | R01 | Delinquent past or present credit obligations with others |
| `payment_burden` | R02 | Excessive obligations in relation to income |
| `income_sufficiency` | R03 | Income insufficient for amount of credit requested |
| `employment_tenure` | R04 | Length of employment |

Up to four are stated, per Reg B (`MAX_REASONS = 4`), ranked worst-first, and all of them are
persisted (`decision_events.principal_reasons`).

**The fail-closed integration gate.** If the model emits any feature with no reason mapping —
including a positive one — the decision is refused rather than issued with a missing or fallback
reason (`UnmappedFeatureError`, surfaced as a typed 503). A model whose vocabulary cannot be
explained does not get to decide. This is the gate any future licensed model must pass.

**Generation never sits on the causal path.** 12 CFR 1002.9 requires the notice to state the
reasons *actually used*; a generated reason is a guess about a decision already made. If drafted
wording is added later, the codes still come from real attributions and the model only phrases
them (carded as G2 in `docs/cards-week8-governance.md`).

## 6. Limitations and caveats

- **A model that reads no protected characteristic can still produce disparate outcomes**, through
  inputs correlated with one. That is why monitoring is planned and why §3's claim, though
  demonstrable, is not sufficient on its own — see §7 and
  `docs/specs/fair-lending-monitoring-week8.md` (**Status: draft**, not yet built).
- **No performance validation exists.** The scorecard is a stand-in with no training data, no
  holdout, and no measured discriminatory power. Its bands come from the client's policy document,
  not from observed default behaviour. It must not be described as validated, calibrated, or
  benchmarked.
- **Pricing is not risk-based today.** Every offer is written at a flat
  `POLICY_RATE_PCT = 7.99` (`services/origination-service/app/routers/offers.py:18`), while
  `policies/fee_schedule.md:29` describes standard note rates of 7.99%–24.99% by risk band. The
  score does not affect price. If banded pricing is implemented, this card and the monitoring spec
  both need revision.
- **Referred applications have no recorded disposition.** Decisioning is automated end to end with
  no override or manual-approval route, so a `refer` leaves the platform without a captured final
  answer — and that is the one path where human discretion would enter.
- **Adverse-action notice delivery cannot be proven.** This platform displays reasons and records
  them; it does not send, track, or time a notice. Carded as G1 in
  `docs/cards-week8-governance.md`.

## 7. Controls around the model

**Active today:**

- KYC must pass before decisioning (ADR 0011).
- Every decision is recorded, identifier-free, with its inputs, drivers, band and reasons
  (`decision_events`).
- Decisions are retained ~25 months per Reg B (`policies/underwriting_guidelines.md`).

**Planned, not active — draft design only:**

- **Fair-lending outcome monitoring does not run today.** Nothing today measures whether
  decision outcomes fall differently across protected groups. The design is specified in
  `docs/specs/fair-lending-monitoring-week8.md` (**Status: draft**), which names the intended
  posture — the reporting environment computes, this platform only emits, so no protected
  characteristic or proxy is stored here — but leaves the reporting environment unnamed, the
  review owner and escalation path unassigned, application volume ungiven, and the geocoding
  source unconfirmed (spec §7, Open Items 1–4). None of the platform's export path exists yet.
  Until those dependencies close and the export ships, this is a design, not a control, and must
  not be cited as one in an examination.

## 8. Review

This card is reviewed when the model changes, when a band moves, when a reason code is added or
retired, and at minimum whenever the monitoring report in §7 opens an inquiry. A licensed model
replacing the stand-in requires a rewrite, not an amendment.
