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
 Borrower / Servicing Rep ─► Next.js portal (3000, published)
                                   │  Authorization: Bearer <session>
                                   ▼
                    gateway / BFF (8000, published)  ── Redis (6379, published)
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

Of the application HTTP services, only `gateway` (8000) and `frontend` (3000) are
published to the host in `docker-compose.yml` (`ports:`); the six backend services are
`expose:`-only on the compose network. (Postgres 5432 and Redis 6379 are separately
published, for local tooling access — not application traffic.) The backend services are
not directly reachable from the host, so the gateway is the sole trust boundary in
practice, even though it does not enforce role authorization on money actions (see *Auth &
roles*). The port numbers in the table below are container-network ports, not host ports.

## Services

| Service | Port | Tech | Owns / Responsibility |
|---------|------|------|-----------------------|
| `gateway` | 8000 | FastAPI + httpx + Redis | Session auth (`/auth/*`), role forwarding, reverse-proxy: `/los/*` → origination, `/lss/*` → servicing, `/kyc/*` → kyc, `/decision/*` → decision, `/disclosure/*` → disclosure, `/payments/*` → payment. Still does **not** enforce role authz on money actions. |
| `origination-service` (LOS) | 8001 | FastAPI + SQLAlchemy + psycopg2 | Application intake & listing (logs an allowlist of fields, with the ADR 0006 redactor as a backstop), LOS→LSS boarding seam (`intake.board_to_servicing`), and **orchestration** — calls kyc/decision/disclosure over synchronous HTTP via `app/clients.py`. The old in-process `apr.py`/`fees.py`/`offer.py`/`decision.py`/`kyc.py` were deleted from here and moved to the new services. Also owns the decisioning assistant (`app/assistant.py`, `app/llm/`, `app/prompts/` — see *Decisioning assistant* below), `app/authz.py` (ADR 0010: officer-OR-owner authorization on application-scoped routes, fail-closed 404), and `app/kyc_gate.py` (ADR 0011: mandatory KYC gate before decision/offer/board). |
| `servicing-service` (LSS) | 8002 | FastAPI + SQLAlchemy + psycopg2 | Loan portfolio, balances, amortization schedule, delinquency/late fees, reconciliation peek, loan reads. New `POST /accounts/{loan_id}/apply-payment` (called by payment-service). Legacy `POST /payments` + `payments.py` are **not** dead: the route is live at `app/main.py:74` and calls `payments.charge`, so two handlers insert into the same `payments` table (D23). |
| `kyc-service` | 8003 | FastAPI + SQLAlchemy + psycopg2 | CIP-only identity check; persists `kyc_checks`. No OFAC/sanctions, no UBO, no ongoing monitoring, no SAR. |
| `decision-service` | 8004 | FastAPI + SQLAlchemy + psycopg2 | Synchronous credit pull + scorecard; persists `decisions` (outcome only) and, since Week 3, an append-only `decision_events` record (inputs, model outputs, reason codes — ADR 0008/0009). Experian + core-banking keys read from the environment with no committed default (`config.py`); the literals were purged in PR #4 and are still owed a rotation (D1). |
| `disclosure-service` | 8005 | FastAPI + SQLAlchemy + psycopg2 | TILA/Reg-Z offer + APR + amortization. The compute path (`apr.py`/`schedule.py`/`offer.py`) is `Decimal` end to end, converting to float only at the response schema and the `offers` columns (ADR 0012). The origination fee has one source, `policies/fee_schedule.json` via `app/rules.py`, which fails closed (no default rate) — replacing three previously-drifted hardcoded copies. Also owns `disclosures`, the authoritative TILA record (integer minor units + `NUMERIC` APR, ADR 0012/0007) and its delivery lifecycle (draft → in_review → approved → delivered, frozen on delivery). |
| `payment-service` | 8006 | FastAPI + SQLAlchemy + psycopg2 | Card/ACH charge. No idempotency (retried POST double-charges); still **stores** full PAN/CVV (D13), though log lines are redacted (ADR 0006); processor key read from the environment with no committed default. After inserting the `payments` row it calls servicing's `apply-payment`. |
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

### Decisioning assistant (LLM)

`origination-service` runs a single-agent decisioning assistant (Week 3, ADR 0005 LLM
client + ADR 0009 design): an LLM agent that orchestrates tools and narrates a decision to
the loan officer, but never scores credit itself — the credit decision stays a
deterministic scoring module, for reproducibility and to avoid a fair-lending "the AI
decided" defense (12 CFR 1002.9). Adverse-action reasons are derived from the model's
actual top negative feature attributions (`app/reasons.py`), not a generic fallback. The
"licensed AI credit-scoring model" itself is hypothetical — no vendor artifact exists; a
deterministic stand-in module emits the vendor output shape behind an interface a real
model can later replace. A RAG corpus-hygiene layer (ADR 0007) and eval harness
(`rag_eval/`, CI job `rag-eval-gate`) support this path.

## Auth & roles

`users` table holds staff + borrower logins (`admin`, `underwriter`, `csr`, `borrower`).
Login → unsalted-sha256 password check → opaque token in Redis (`session:<token>`, 8h
TTL). The gateway resolves the session and forwards `X-User-Id` / `X-User-Role`.
Downstream services still **do not enforce role** on balance adjustments or fee waivers
(servicing-service). `origination-service` is the exception: `app/authz.py` (ADR 0010)
enforces officer-OR-owner authorization on application-scoped routes, fail-closed 404 on
mismatch, and `app/kyc_gate.py` (ADR 0011) requires a passed KYC check before
decision/offer/board can proceed.

## Data model (Postgres)

`users`, `applicants`, `applications`, `kyc_checks`, `decisions`, `offers`, `loans`,
`balances`, `payments`, `audit_logs`, plus two Week 3+ additions: `decision_events`
(append-only decisioning record — inputs, model outputs, reason codes, reason texts;
ADR 0008/0009) and `disclosures` (authoritative TILA record, ADR 0012/0007). A provenance
chain runs `applicants → applications → decision_events → offers → disclosures`, surfaced
via the view `v_disclosure_provenance`. Authoritative DDL: `db/init/001_schema.sql`. Seed:
`db/init/002_seed.sql` (curated anchors) + `db/init/003_seed_bulk.sql` (synthetic
portfolio of ~300 applications / ~180 loans / ~600 payments). Migrations under
`db/migrations/` (16 files) are hand-tracked and lag the init DDL.

Money is stored as `DOUBLE PRECISION` in `offers`, `loans`, `balances`, `payments` — except
`disclosures`, which stores money as `BIGINT` minor units + `NUMERIC(9,3)` APR (ADR 0012's
Decimal/minor-units beachhead, deliberately contrasted with `offers`' float). `balances` is
a single mutable column (no ledger). `decisions` records the outcome only (now written by
`decision-service`); `decision_events` records the full decisioning trail. `payments`
carries the full PAN + CVV and has no idempotency key (now written by `payment-service`;
ADR 0013 proposes payment idempotency/tokenization but is not yet implemented). Decomposition
did not change the schema — all seven services share the same `db/init` tables. See ADR
0002 / 0003 for the original rationale and ADR 0004 for the decomposition.

## The LOS↔LSS seam

A funded loan is "boarded" by a direct cross-schema `INSERT` from origination into the
servicing `loans` + `balances` tables (`origination-service/app/intake.py::board_to_servicing`).
No boarding API, event, or contract. ADR 0002.

A second cross-service write now exists on the servicing side: after `payment-service`
captures a charge and inserts the `payments` row, it calls `servicing POST
/accounts/{loan_id}/apply-payment` to post the payment against the balance. The
balance-mutation debt (race / lost-update, mutable balance, no payment waterfall, no
maker-checker) lives behind that endpoint and is unchanged.

## ADRs

`adr/0001` through `adr/0013`. Beyond the decomposition-era 0002 (single shared DB), 0003
(store card data), 0004 (service decomposition): 0005 (LLM client design), 0006 (logging
redaction), 0007 (RAG corpus hygiene), 0008 (retrievable decision records), 0009
(decisioning assistant design), 0010 (application ownership authorization), 0011
(mandatory KYC before decisioning), 0012 (Decimal minor units + externalized rule config),
0013 (payment idempotency/tokenization — proposed, not yet implemented).

## Status of major controls

| Control | Status | Detail |
|---------|--------|--------|
| PII redactor consistency | Implemented | `redactor-drift` + `redaction-tests`, blocking |
| Origination application authz (ADR 0010) | Implemented | officer-OR-owner, fail-closed 404 |
| Servicing role authz | Partially implemented | `app/authz.py` role-gates `adjust-balance` and `waive-fee` and owner-gates the loan reads (#32); maker-checker is unbuilt (ADR 0017, Proposed) and the gateway still enforces no role authz |
| Settlement reconciliation (ADR 0015) | Implemented, ungated | `app/reconciliation.py` compares captures to settlement and classifies breaks; its suite has **no** blocking CI job |
| Mandatory KYC gate (ADR 0011) | Implemented | blocks decision/offer/board pre-KYC |
| Disclosure Decimal/minor units (ADR 0012) | Partially mitigated | `disclosures` is Decimal/`BIGINT`; `offers`/`loans`/`balances`/`payments` still `DOUBLE PRECISION` |
| Origination fee externalized config | Implemented | `policies/fee_schedule.json`, fails closed |
| TILA APR compute + vector gate | Implemented | `tila-vectors-gate`, blocking, pinned vector literals |
| Decisioning assistant (LLM narrates, never scores) | Implemented | ADR 0009; credit decision stays deterministic |
| Payment idempotency / tokenization (ADR 0013) | Proposed, not implemented | no idempotency key on `payments`; full PAN+CVV stored |
| DB readiness gates | Implemented | `db-readiness-gate`, `decision-db-readiness-gate` |
| LOS↔LSS boarding contract | Not implemented | direct cross-schema `INSERT`, no API/event (ADR 0002) |

## CI gates (`.github/workflows/ci.yml`)

The `backend` matrix job runs pytest with `continue-on-error` — money-math test failures
there do not block the build (known-flaky, tolerated). Everything else listed is a
**blocking** job: `redactor-drift`, `redaction-tests`, `synthetic-credit-gate`,
`decision-idempotency-gate`, `offer-guard-gate`, `adr-0010-authz-gate`,
`gateway-trust-boundary-gate`, `compose-hardening-gate`, `kyc-enforcement-gate`,
`tila-vectors-gate`, `db-readiness-gate`, `decision-db-readiness-gate`,
`migration-numbering-gate`, `disclosure-lifecycle-gate`, `rag-eval-gate`, `frontend`,
`secret-scan`, `doc-path-lint`, `docs-drift`, `spec-diff-gate`. Three of those carry a
companion self-test job (`doc-path-lint-tests`, `docs-drift-tests`, `spec-diff-gate-tests`)
that asserts the gate's own logic
against a throwaway fixture, independent of the docs' actual state; `redaction-tests` is
not a self-test of `redactor-drift` — it is its own independent blocking gate on the
redaction logic. See `CLAUDE.md` for the exception to the tolerated-money-math rule
(`tila-vectors-gate`) and what each gate protects.

## Local development

`docker compose up -d` brings up Postgres (auto-seeds from `db/init`), Redis, the gateway,
the six backend services, and the frontend. See `docs/runbook.md`.
