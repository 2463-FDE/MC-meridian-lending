# ADR 0011: Mandatory Passing KYC Before Decisioning, Offer, and Boarding

- **Status:** **Accepted** — built in the Week-3 review (fail-closed KYC gate on the
  regulated/money routes).
- **Date:** 2026-07-17
- **Author:** Claude Code
- **Related:** ADR 0010 (application ownership + anonymous continuation token — this gate
  sits directly behind it on the same routes), ADR 0004 (kyc-service decomposition),
  the round-8 KYC observability work (kyc_checked flag + kyc_unavailable audit row),
  debt D11 (CIP-only KYC: no sanctions/OFAC, no UBO, no ongoing monitoring)

---

## Context

KYC was **advisory**. `POST /applications` (submit) ran a CIP check via kyc-service and
recorded the outcome (round 8 made a failure observable: `kyc_checked=false` + a
`kyc_unavailable` audit row), but **nothing downstream enforced it**. The
application-scoped routes checked authorization (ADR 0010), an approved decision, and offer
existence — never identity verification.

ADR 0010 Phase B then let a logged-out applicant complete decision → offer → accept using
the continuation token issued at submit. Combined with advisory KYC, an applicant could
submit while kyc-service was **down** (no `kyc_checks` row written), or with a **declined**
CIP result, then use the token to pull credit, generate a TILA offer, and board a funded
loan — origination without completed identity verification. A review round flagged this as
high severity.

This was deferred once (round 12) as "mandatory-KYC = product + compliance, availability
tradeoff." This ADR takes that decision: **fail closed.**

## Decision

**Require a passing CIP/KYC check before `POST /applications/{id}/decision`, `POST /offer`,
and `POST /applications/{id}/accept`.** Implemented as
`origination-service/app/kyc_gate.py::require_kyc_passed(app_id)`, called on each of the
three routes immediately after the ADR 0010 authorization check.

- **Pass definition mirrors kyc-service's own** — `name_verified AND address_verified`
  (see `kyc-service/app/routers/kyc.py::cip_passed`). Origination does not invent a
  stricter or looser rule than the service that performs the check. Entity applicants
  (no dob/ssn) pass on name+address exactly as kyc-service already allows (debt D11 carried
  forward — this gate enforces the existing CIP result, it does not add sanctions/UBO).
- **Authoritative source is the persisted `kyc_checks` row**, not submit's response — the
  gate reads the latest `kyc_checks` for the application's applicant.
- **Fails closed.** A failing latest row (CIP declined) OR no row at all (the check never
  ran, e.g. kyc-service was unavailable at submit) blocks with **409** — the same
  state-not-satisfied code the ADR 0010-alt-3 decision-state guards already use. Ordered
  after authorization, so an unauthorized caller still gets 404 (no KYC-state oracle)
  before any 409.
- **Defense in depth.** Gating `decision` alone would transitively block offer/accept (they
  require an approved decision), but all three are gated so a funded loan can never board on
  an unverified identity even if an approved decision somehow predates the gate.
- **Parity sweep (every decision/board trigger).** The AI assistant's score tool
  (`assistant.py::_score_application`) pulls a decision via decision-service exactly like
  the manual officer route, so it is gated too — otherwise the assistant would be a KYC
  bypass. The legacy `POST /board` hatch is deliberately **exempt**: it is internal-only
  (X-Internal-Service, gateway-stripped), takes fully caller-supplied inputs with no LOS
  lookup, and has no product caller (parity with its ADR 0010 decision-state-guard
  exemption); the product boarding path `/accept` is gated. Direct `POST /decisions` on
  decision-service is likewise internal-only; KYC gating lives at the origination
  orchestration layer.

## Consequences

### Positive

- A declined or never-run identity check can no longer reach a credit pull, a TILA offer,
  or a funded loan — closes the anonymous self-funding-without-KYC path ADR 0010 Phase B
  otherwise widened.
- No schema change: the gate reads existing `kyc_checks` columns.
- Blocking CI coverage (`kyc-enforcement-gate`) proves decision/offer/accept 409 on a
  declined or absent KYC.

### Negative / tradeoff (accepted)

- **Availability:** during a kyc-service outage, applications can still be *submitted* but
  cannot *advance* (decision/offer/accept 409) until a passing check exists. This is the
  deliberate fail-closed posture — an identity outage stops originations rather than
  originating unverified loans.
- **Recovery today is re-submit:** there is no re-run-KYC endpoint, so an application
  stranded by an outage is re-driven by a fresh submit (which re-invokes kyc-service). A
  dedicated re-KYC / remediation route (mirroring the monthly_debt capture escape hatch) is
  a reasonable follow-up, out of scope here.
- **CIP-only depth is unchanged (debt D11):** this gate enforces the *existing* CIP result;
  it does not add sanctions/OFAC, UBO, or ongoing monitoring. Those remain open debt.

## Alternatives considered

1. **Block confirmed declines only, allow KYC-unavailable through** — preserves
   availability during an outage, but an attacker who can force kyc-service unavailable
   could then self-fund. Rejected: the outage path is exactly the one an attacker would
   drive, so allowing it defeats the control.
2. **Keep KYC advisory, rely on officers to catch it** — the status quo the review
   flagged; no enforcement, and the anonymous token path has no officer in the loop.
   Rejected.
3. **Gate only `accept` (the money action)** — smaller, but would still allow a credit
   pull and a persisted offer on an unverified identity (regulated actions in their own
   right). Rejected in favor of gating all three.

## Sign-off status

| Owner (role) | Status |
|--------------|--------|
| Engineering | **Accepted** — fail-closed gate on decision/offer/accept, mirroring kyc-service's pass rule; blocking CI gate |
| Product | **Accepted** — fail-closed chosen over the availability of originating during a KYC outage |
| Compliance / Legal | **Informational** — identity verification is now enforced before credit pull / offer / funding; CIP depth (no sanctions/UBO) remains open debt D11 |
