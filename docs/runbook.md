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

The backend services (8001–8006) are NOT host-published — the gateway (:8000) is the sole
external entry (PR review: a direct host port would let a caller forge X-User-Role and
bypass the officer gate). Reach a service's /health from inside the network via
`docker compose exec`:

```bash
curl localhost:8000/health                                        # gateway (published)
docker compose exec origination-service curl -s localhost:8001/health   # LOS
docker compose exec servicing-service   curl -s localhost:8002/health   # LSS
docker compose exec kyc-service          curl -s localhost:8003/health
docker compose exec decision-service     curl -s localhost:8004/health
docker compose exec disclosure-service   curl -s localhost:8005/health
docker compose exec payment-service      curl -s localhost:8006/health
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

### Idempotent decisions

`POST /los/applications/{id}/decision` accepts an `Idempotency-Key` header (forwarded to
`decision-service` as `request_id`). A retry with the SAME key replays the recorded
decision — no second bureau pull, no second `decision_events` row. The borrower portal
sends a stable per-application key automatically; officer/ops callers should send their
own on retryable requests. A key reused with DIFFERENT decision inputs (amount, income,
term, monthly_debt, employment_years, or SSN) returns **409** rather than a stale replay.

- **`DECISION_FINGERPRINT_PEPPER` (decision-service, env only).** SSN drives the bureau
  pull, so the SSN is part of that conflict check — but only via a keyed HMAC (the raw
  SSN is never persisted). This pepper is the HMAC key and **must be a real secret**: the
  digest is only non-reversible while the pepper is secret, and an SSN is a 9-digit space,
  so a public/placeholder pepper lets anyone with `decision_events` access brute-force the
  fingerprints back to SSNs. So:
  - `.env.example` ships it **blank** (no committed value — same posture as
    `INTERNAL_SERVICE_TOKEN` / `EXPERIAN_KEY`). Set a real secret from a secret-manager in
    any non-dev deploy.
  - A blank or known-placeholder value is treated as **no pepper**: no fingerprint is
    persisted, and **outside development `/health` reports unhealthy** (it is in
    `missing_required_secrets`). SSN-change detection then degrades to the financial-input
    fields only.
  - The **local demo** supplies a dev-only value via `docker-compose.demo.yml`
    (`ENVIRONMENT=development`, synthetic SSNs), so the check runs in the demo without a
    committed production secret.
  - Rotating it invalidates in-flight fingerprints, so a retry mid-rotation may 409 (fails
    safe — never a stale decision).

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
