# ADR 0004: Decompose the origination monolith into focused services

- **Status:** Accepted
- **Date:** 2026-06-29
- **Author:** In-house team

## Context

The platform Halcyon delivered ran as three backend services — `gateway`,
`origination-service` (LOS), and `servicing-service` (LSS). Over time `origination-service`
accreted most of the lending logic: application intake, the KYC/CIP check, the credit pull
+ scorecard, and TILA/Reg-Z disclosure all lived in-process (`kyc.py`, `decision.py`,
`apr.py`, `fees.py`, `offer.py`). Charge capture lived inside servicing. The platform's
intended target architecture (and the org chart that owns it) splits these into separate
services, and we wanted parity with that shape so the right team owns each concern.

## Decision

Extract four standalone FastAPI services from the existing code:

- **`kyc-service`** (8003) — CIP identity check; owns `kyc_checks`.
- **`decision-service`** (8004) — synchronous credit pull + scorecard; owns `decisions`.
- **`disclosure-service`** (8005) — TILA/Reg-Z offer + APR + amortization.
- **`payment-service`** (8006) — card/ACH charge capture.

`origination-service` becomes an intake + boarding **orchestrator**: it keeps application
intake and the LOS→LSS boarding seam, and calls kyc/decision/disclosure over synchronous
HTTP (`app/clients.py`, base URLs from `KYC_URL` / `DECISION_URL` / `DISCLOSURE_URL`).
`payment-service` calls a new servicing endpoint `POST /accounts/{loan_id}/apply-payment`
to post a captured charge against the balance. The gateway gains routes `/kyc/*`,
`/decision/*`, `/disclosure/*`, and `/payments/*`. The old in-process modules were deleted
from origination and moved into the new services. The data layer is unchanged — all seven
services share the one Postgres database and the same `db/init` schema + seed (ADR 0002).

## Consequences

- **Pro:** each concern is a deployable unit owned by the right team; the topology now
  matches the platform's intended architecture; the `gateway` is the single front door for
  all seven services.
- **Con — synchronous coupling got worse.** The in-process calls became synchronous
  **HTTP** calls with no timeout/retry/circuit-breaker contract. A `decision-service` stall
  (its credit pull blocks the thread) now blocks the applicant-facing origination request
  waiting on it. We amplified the existing synchronous-decisioning flaw, now across a
  network hop.
- **Con — the debt moved, it did not get fixed.** Float money math and the drifted
  origination-fee constants (0.025 / 0.030 / 0.03) moved into `disclosure-service`;
  no-idempotency double-charge and full PAN/CVV logging+storage moved into `payment-service`;
  outcome-only decisions, the generic "purchasing history" adverse-action reason, and the
  hardcoded Experian/core-banking keys moved into `decision-service`; the
  no-OFAC/no-UBO/no-monitoring/no-SAR gaps moved into `kyc-service`. None of these were
  remediated as part of the extraction.
- **Con — the migration is partial.** Services still reach into the one shared schema
  rather than owning their data; the LOS→LSS boarding seam is still a direct cross-schema
  `INSERT`; the legacy `POST /payments` + `payments.py` in servicing are dead-but-present;
  and the "AI underwriting assistant" / ML scorer remains unstarted greenfield.
