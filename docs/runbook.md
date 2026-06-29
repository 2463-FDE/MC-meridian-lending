# Meridian Lending — Operations Runbook

> In-house ops notes. Sparse — Halcyon left no runbook, so this is what we've pieced
> together. Add to it when you learn something the hard way.

## Local / dev bring-up

```bash
cp .env.example .env     # NOTE: a populated .env is already committed, so this is optional
make up                  # docker compose up -d --build (postgres, redis, services, frontend)
make logs                # tail all services
make ps                  # container status
make down                # stop everything
```

- Portal: http://localhost:3000
- Gateway + OpenAPI docs: http://localhost:8000/docs
- Postgres: localhost:5432 (`meridian` / see `.env`)
- The DB auto-seeds from `db/init/*.sql` on first `up` (fresh volume only).

To re-apply the curated seed without recreating the volume:
```bash
make seed
```

To wipe and re-seed from scratch:
```bash
docker compose down -v && make up
```

## Demo logins

All seeded with password `password`:

| Username | Role | Use |
|----------|------|-----|
| `admin` | admin | full portal |
| `underwriter` | underwriter | decisioning views |
| `csr` | csr | servicing dashboard |
| `maria` | borrower | borrower view (applicant #1) |

## Health checks

```bash
curl localhost:8000/health     # gateway
curl localhost:8001/health     # origination (LOS, intake + boarding orchestrator)
curl localhost:8002/health     # servicing (LSS)
curl localhost:8003/health     # kyc-service
curl localhost:8004/health     # decision-service
curl localhost:8005/health     # disclosure-service
curl localhost:8006/health     # payment-service
```

Ports 8003–8006 are the four services extracted from the old origination monolith
(ADR 0004). `.env` carries their base URLs as `KYC_URL` / `DECISION_URL` /
`DISCLOSURE_URL` / `PAYMENT_URL` — origination reads these in `app/clients.py`.

## Common tasks

Endpoints are reached through the gateway. After the decomposition, decisioning,
disclosure, KYC, and payments are backed by their own services — origination still
orchestrates the LOS flow and calls them over HTTP.

- **Run a credit decision:** `POST /los/applications/{id}/decision` (origination orchestrates
  → `decision-service`), or hit `decision-service` directly via `/decision/*`.
- **Run a KYC/CIP check:** `/kyc/*` → `kyc-service` (origination also calls it inline during intake).
- **Generate an offer/disclosure:** `POST /los/offer {app_id, principal, annual_rate_pct, term_months}`
  (origination → `disclosure-service`), or `/disclosure/*` directly.
- **Board an approved app to servicing:** `POST /los/applications/{id}/accept`.
- **Take a payment:** `/payments/*` → `payment-service` (captures the charge, then calls
  servicing `POST /accounts/{loan_id}/apply-payment` to post it). The legacy `POST /lss/payments`
  path is dead-but-present.
- **Look at the portfolio:** `GET /lss/loans?limit=25&offset=0&status=current` (requires auth).
- **Reconciliation eyeball:** `GET /lss/reconciliation/peek` (ledger vs settlement totals).

## Known operational pain (unresolved)

- **Payment retries.** The processor occasionally times out; clients retry. `payment-service`
  has no idempotency key, so retried payments insert a second row and apply twice (the second
  `apply-payment` call posts again). We field "double charge" support tickets a few times a
  month. (No fix yet — moved with the code into `payment-service`.)
- **Decision/disclosure/KYC stalls block applicants.** Origination calls these over
  synchronous HTTP with no timeout or retry. If `decision-service`'s credit pull hangs, the
  applicant-facing origination request hangs with it. Watch `decision-service` latency when
  intake requests pile up. (No circuit breaker / fallback.)
- **Month-end close.** `reconciliation.peek` totals do not tie out and nothing runs on a
  schedule. Finance reconciles by hand in a spreadsheet.
- **Logs contain card + SSN data.** `payment-service` logs full PAN/CVV/SSN at INFO to
  `logs/payment-service.log` (and origination still logs full PII at intake). Do not ship
  these logs to a third-party aggregator until redaction is added.
- **Secrets are in the repo.** `.env` is committed and the services' `config.py` hardcode
  fallbacks — including Experian/core-banking keys in `decision-service` and the processor
  key in `payment-service`. Rotate before any real go-live. (Long-standing TODO.)

## Tests

```bash
make test    # runs pytest in both backend services (non-blocking)
```

Some money-math tests (`test_apr.py`, `test_money.py`) currently FAIL by design — they
encode the float-rounding defects we have not fixed. CI runs them `continue-on-error`.
