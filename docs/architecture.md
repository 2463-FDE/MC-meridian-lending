# Meridian Lending — Architecture Notes

*(Reconstructed by the in-house team after Halcyon dissolved. Incomplete.)*

## The two halves (the "seam")

Meridian is really **two systems bolted together** — and since the in-house team began the
decomposition (ADR 0004) the two halves are each fronted by a small set of extracted
services. Seven backend services total: gateway, the LOS orchestrator, the LSS, and four
extracted services (kyc, decision, disclosure, payment).

- **LOS — Loan Origination System** (`origination-service`): everything up to funding.
  application intake → KYC (CIP) → credit pull + decisioning → disclosure/offer. Intake and
  the boarding seam still live here; KYC, decisioning, and disclosure were extracted into
  `kyc-service` (8003), `decision-service` (8004), and `disclosure-service` (8005), which
  origination now calls over synchronous HTTP (`app/clients.py`).
- **LSS — Loan Servicing System** (`servicing-service`): everything after funding.
  balances → delinquency/late fees → reconciliation. The card/ACH charge was extracted into
  `payment-service` (8006); servicing exposes `apply-payment` to post a captured charge.

```
  applicant ──► frontend ──► gateway ──► origination-service (LOS)
                                              │   sync HTTP (clients.py)
                                  ┌───────────┼───────────┐
                                  ▼           ▼           ▼
                            kyc-service  decision-svc  disclosure-svc
                              (8003)       (8004)        (8005)
                                              │
                                              │  "boarding" — a loan that is
                                              ▼  approved+funded is handed to LSS
                                        servicing-service (LSS) ◄── apply-payment ── payment-service (8006)
                                              │
                                              ▼
                                          Postgres (shared by all 7)
```

### The seam is undocumented and direct

When a loan is approved and funded, origination "boards" it to servicing by writing
directly into the servicing tables (`loans`, `balances`) — there is no boarding API,
no event, no contract. The two services share one Postgres database and reach across
each other's schemas. See `origination-service/app/intake.py::board_to_servicing`.

This is **brownfield seam #1**: the systems were integrated by a sales-driven vendor
to "make the demo work," not designed.

### Seam #2 — the new synchronous service mesh

The decomposition added a second class of seam: origination → kyc/decision/disclosure and
payment → servicing are **synchronous HTTP** calls with no timeout/retry contract. A stall
in `decision-service` (its credit pull blocks the thread) now blocks the applicant-facing
origination request waiting on it — the old synchronous-decisioning flaw, now across a
network hop. None of the extracted services own their own datastore; they all reach into
the one shared Postgres.

## Data stores

- **Postgres** — users, applicants, applications, kyc_checks, decisions, offers, loans,
  balances, payments, audit_logs. **Shared by all seven services** — the decomposition did
  not split the schema; the extracted services reach into the same tables (kyc → kyc_checks,
  decision → decisions, disclosure → offers, payment → payments).
- **Redis** — login sessions (gateway). Also intended for idempotency keys / jobs —
  never wired up (so `payment-service` still has no idempotency).

## Auth

The gateway handles login (`/auth/*`) against the `users` table, mints an opaque session
token stored in Redis, and forwards the resolved identity downstream as `X-User-*`
headers. Downstream services trust those headers and do **not** re-check role on
money-moving actions (the weak-authz gap).

## External integrations

These moved with the decomposition:

- **Experian** — credit pull, now in `decision-service` (key still hardcoded, now in
  `decision-service/app/config.py`, alongside a hardcoded core-banking key).
- **Card/ACH processor** — charge capture, now in `payment-service` (processor key
  hardcoded in its config).

## Known unknowns (things the team has not had time to map)

- How balances are recomputed after a partial payment + a fee waiver on the same day —
  now mediated by servicing's `apply-payment`, called by `payment-service`.
- Whether the processor settlement file actually ties out to the `payments` table.
- Where adverse-action *reasons* are stored (we think… nowhere?) — `decision-service`
  still records outcome only and returns a generic "purchasing history" reason.
- Whether any servicing action is actually restricted by role (gateway still does not
  enforce authz on money actions, across any of the seven services).
- What happens when one of the new sync-HTTP calls (kyc/decision/disclosure, or
  payment→servicing) times out or half-fails — there is no timeout, retry, or compensation
  contract, and origination has no fallback if `decision-service` hangs.
