# Meridian Lending Platform

> **SOC 2 Type II / SOX-controlled. Cardholder data is encrypted and we are PCI-DSS compliant. Every credit decision is audited.**

The Meridian Lending Co. loan origination + servicing platform. Originally delivered by
Halcyon Software Group (now dissolved) as **three** backend services — `gateway`,
`origination-service` (LOS), `servicing-service` (LSS); maintained in-house since 2024-Q4.

This is a brownfield monorepo: a **Loan Origination System (LOS)** and a **Loan Servicing
System (LSS)** bolted together behind a single API gateway, with a Next.js borrower +
servicing portal. (Lending Ops keeps asking for an "AI underwriting assistant" — that
work has not been started.)

Since the handoff the in-house team has begun **extracting the LOS monolith into focused
services**, partly to match the platform's intended target architecture. The platform now
runs **seven** backend services: the original three plus four extracted ones —
`kyc-service`, `decision-service`, `disclosure-service`, and `payment-service`.
Origination is now an intake + boarding **orchestrator** that calls the new KYC, decision,
and disclosure services over synchronous HTTP; the old in-process `apr.py` / `fees.py` /
`offer.py` / `decision.py` / `kyc.py` modules moved out with them. This modernization is
**partial** — the data layer and most of the money-handling debt moved with the code
rather than being fixed.

## Architecture

```
                          ┌──────────────────────┐
  Next.js portal  ───────►│   gateway (BFF)      │  :8000
  (apply + servicing)     │   session auth/roles │
                          └─────────┬────────────┘
                                    │  /auth · /los · /lss · /kyc
                                    │  /decision · /disclosure · /payments
        ┌───────────────────────────┼───────────────────────────┐
        ▼                                                       ▼
 origination-service                                    servicing-service
   :8001  (LOS)                                           :8002  (LSS)
   intake + boarding orchestrator                         balances / delinquency /
        │  (sync HTTP, app/clients.py)                    reconciliation / loan reads
        ├──────────────┬──────────────┐                          ▲
        ▼              ▼              ▼                           │ apply-payment
   kyc-service   decision-service  disclosure-service             │
     :8003          :8004             :8005                  payment-service
   CIP identity   credit pull +     TILA/Reg-Z offer            :8006
                  scorecard         APR + amortization       card/ACH charge ─┘
        │              │              │                           │
        └──────────────┴──────────────┴───────────┬───────────────┘
                                                   ▼
                      Postgres :5432 (shared)  +   Redis :6379 (sessions)
```

All seven services share **one** Postgres database and the same `db/init` schema + seed —
the data layer is unchanged by the decomposition. The LOS↔LSS **seam** is still thin and
undocumented — a loan "boards" from origination to servicing by a direct insert into the
servicing schema. After a charge is captured, `payment-service` calls servicing's
`apply-payment` to post it. See `docs/architecture.md`.

## Quick start

```bash
cp .env.example .env     # the real .env is already in the repo so you can just run it
make up                  # docker compose up -d (postgres, redis, all services, frontend)
make logs                # tail everything
make seed                # load db/init seed data (loans, payments, decisions)
make down
```

Portal: http://localhost:3000  ·  Gateway: http://localhost:8000/docs

Demo logins (all seeded with password `password`): `admin`, `underwriter`, `csr`,
and a borrower login `maria`.

## Services

| Path | Service | Port | Notes |
|------|---------|------|-------|
| `frontend/` | Next.js 15 portal | 3000 | application wizard + servicing dashboard |
| `services/gateway/` | FastAPI BFF | 8000 | session auth/roles; routes to LOS/LSS + KYC/decision/disclosure/payments |
| `services/origination-service/` | FastAPI (LOS) | 8001 | intake + LOS→LSS boarding orchestrator; calls KYC/decision/disclosure over HTTP |
| `services/servicing-service/` | FastAPI (LSS) | 8002 | balances, schedule, delinquency, reconciliation, `apply-payment` |
| `services/kyc-service/` | FastAPI | 8003 | CIP identity check; persists `kyc_checks` |
| `services/decision-service/` | FastAPI | 8004 | synchronous credit pull + scorecard; persists `decisions` (outcome only) |
| `services/disclosure-service/` | FastAPI | 8005 | TILA/Reg-Z offer + APR + amortization |
| `services/payment-service/` | FastAPI | 8006 | card/ACH charge; posts to servicing via `apply-payment` |
| `db/` | Postgres init + seed | 5432 | schema, migrations, seed data (shared by all services) |

## Compliance

We are PCI-DSS compliant (cardholder data encrypted), SOX-controlled with full audit
trails on financial actions, and follow ECOA/Reg B for all adverse-action notices.
Compliance contact: Dana (VP Lending Ops). For SOX/reconciliation questions: Sam
(Controller). For fair-lending/BSA: Priya (Compliance Officer).

## Known follow-ups (from the Halcyon handoff note)

> "Platform is secure and compliant. A few TODOs left in servicing but nothing
> blocking. — Halcyon"
