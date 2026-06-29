# Meridian Lending — Architecture

> Maintained by the in-house team. The platform was originally delivered by Halcyon
> Software Group (dissolved) and has been extended in-place since. Treat this as the
> current best understanding, not a clean-room design.

## System shape

Meridian is a consumer **personal-installment-loan** platform: two domains (origination
and servicing) bolted together behind one BFF gateway, with a Next.js portal. The original
three services (gateway, LOS, LSS) have since been decomposed — the in-house team extracted
KYC, decisioning, disclosure, and payments into standalone services (ADR 0004). There are
now **seven** backend services; origination is an intake + boarding orchestrator that fans
out to the new services over synchronous HTTP.

```
 Borrower / Servicing Rep ─► Next.js portal (3000)
                                   │  Authorization: Bearer <session>
                                   ▼
                          gateway / BFF (8000)  ── Redis (sessions)
                  /auth · /los · /lss · /kyc · /decision · /disclosure · /payments
                     ┌───────────┴────────────────────────────────┐
                     ▼                                             ▼
        origination-service (8001)                      servicing-service (8002)
        LOS: intake + LOS→LSS boarding                  LSS: loans, balances,
        orchestrator (sync HTTP, clients.py)            schedule, delinquency,
                     │                                  reconciliation, apply-payment
        ┌────────────┼─────────────┐                              ▲
        ▼            ▼             ▼                               │ POST apply-payment
  kyc-service  decision-service  disclosure-service                │
    (8003)        (8004)           (8005)                   payment-service (8006)
  CIP identity  credit pull +    TILA/Reg-Z offer          card/ACH charge ───────┘
                scorecard        APR + amortization
                     └────────────┴─────────────┬────────────────┘
                                                 ▼
                                          Postgres (5432, shared)
```

## Services

| Service | Port | Tech | Owns / Responsibility |
|---------|------|------|-----------------------|
| `gateway` | 8000 | FastAPI + httpx + Redis | Session auth (`/auth/*`), role forwarding, reverse-proxy: `/los/*` → origination, `/lss/*` → servicing, `/kyc/*` → kyc, `/decision/*` → decision, `/disclosure/*` → disclosure, `/payments/*` → payment. Still does **not** enforce role authz on money actions. |
| `origination-service` (LOS) | 8001 | FastAPI + SQLAlchemy + psycopg2 | Application intake & listing (still logs full PII), LOS→LSS boarding seam (`intake.board_to_servicing`), and **orchestration** — calls kyc/decision/disclosure over synchronous HTTP via `app/clients.py`. The old in-process `apr.py`/`fees.py`/`offer.py`/`decision.py`/`kyc.py` were deleted from here and moved to the new services. |
| `servicing-service` (LSS) | 8002 | FastAPI + SQLAlchemy + psycopg2 | Loan portfolio, balances, amortization schedule, delinquency/late fees, reconciliation peek, loan reads. New `POST /accounts/{loan_id}/apply-payment` (called by payment-service). Legacy `POST /payments` + `payments.py` remain dead-but-present. |
| `kyc-service` | 8003 | FastAPI + SQLAlchemy + psycopg2 | CIP-only identity check; persists `kyc_checks`. No OFAC/sanctions, no UBO, no ongoing monitoring, no SAR. |
| `decision-service` | 8004 | FastAPI + SQLAlchemy + psycopg2 | Synchronous credit pull + scorecard; persists `decisions` (outcome only). Experian + core-banking keys hardcoded in its `config.py`. No ML/LLM — the "AI scorer" is still greenfield. |
| `disclosure-service` | 8005 | FastAPI + SQLAlchemy + psycopg2 | TILA/Reg-Z offer + APR + amortization. Float money math; origination-fee % hardcoded and drifted (`apr.py` 0.025 / `fees.py` 0.030 / `offer.py` 0.03). |
| `payment-service` | 8006 | FastAPI + SQLAlchemy + psycopg2 | Card/ACH charge. No idempotency (retried POST double-charges); logs + stores full PAN/CVV; processor key hardcoded. After inserting the `payments` row it calls servicing's `apply-payment`. |
| `frontend` | 3000 | Next.js 15 (App Router) | Borrower application wizard, offer/disclosure screen, servicing dashboard + loan detail. |

### Data access — a partial ORM migration

Read paths (loan/application listing, detail, schedule, payment history) use **SQLAlchemy
2.0** ORM models (`models.py` + `database.py`). The older money-moving write paths
(`intake.py`, decisioning, payments, `balance.py`) still use **raw psycopg2** (`db.py`).
The migration to the ORM was never finished — this seam is intentional and is where most
of the money-handling debt lives. The service decomposition (ADR 0004) did **not** clean
this up: the write-path code moved into `decision-service` / `disclosure-service` /
`payment-service` carrying the same raw-psycopg2 + float-money patterns, and every service
still talks to the one shared schema directly.

### Service-to-service wiring — a new synchronous coupling

Origination no longer decides, discloses, or KYCs in-process. It now calls `kyc-service`,
`decision-service`, and `disclosure-service` over **synchronous HTTP** (`app/clients.py`),
and `payment-service` calls servicing's `apply-payment` to post a captured charge. This
re-creates the original synchronous-chain debt at a worse altitude: a downstream
`decision-service` stall (its credit pull blocks the thread) now blocks the
**applicant-facing** origination request that is waiting on the HTTP call — the same
"synchronous decisioning chain" flaw, now spanning a network hop with no timeout/retry
contract.

## Auth & roles

`users` table holds staff + borrower logins (`admin`, `underwriter`, `csr`, `borrower`).
Login → unsalted-sha256 password check → opaque token in Redis (`session:<token>`, 8h
TTL). The gateway resolves the session and forwards `X-User-Id` / `X-User-Role`.
Downstream services accept those headers and **do not enforce role** on balance
adjustments or fee waivers.

## Data model (Postgres)

`users`, `applicants`, `applications`, `kyc_checks`, `decisions`, `offers`, `loans`,
`balances`, `payments`, `audit_logs`. Authoritative DDL: `db/init/001_schema.sql`. Seed:
`db/init/002_seed.sql` (curated anchors) + `db/init/003_seed_bulk.sql` (synthetic
portfolio of ~300 applications / ~180 loans / ~600 payments). Migrations under
`db/migrations/` are hand-tracked and lag the init DDL.

Money is stored as `DOUBLE PRECISION` throughout. `balances` is a single mutable column
(no ledger). `decisions` records the outcome only (now written by `decision-service`).
`payments` carries the full PAN + CVV and has no idempotency key (now written by
`payment-service`). Decomposition did not change the schema — all seven services share the
same `db/init` tables. See ADR 0002 / 0003 for the original rationale and ADR 0004 for the
decomposition.

## The LOS↔LSS seam

A funded loan is "boarded" by a direct cross-schema `INSERT` from origination into the
servicing `loans` + `balances` tables (`origination-service/app/intake.py::board_to_servicing`).
No boarding API, event, or contract. ADR 0002.

A second cross-service write now exists on the servicing side: after `payment-service`
captures a charge and inserts the `payments` row, it calls `servicing POST
/accounts/{loan_id}/apply-payment` to post the payment against the balance. The
balance-mutation debt (race / lost-update, mutable balance, no payment waterfall, no
maker-checker) lives behind that endpoint and is unchanged.

## Local development

`docker compose up -d` brings up Postgres (auto-seeds from `db/init`), Redis, both
backend services, the gateway, and the frontend. See `docs/runbook.md`.
