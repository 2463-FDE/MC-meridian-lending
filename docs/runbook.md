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
- **Reconcile captures against settlement:** `python -m app.reconcile` on `servicing-service`
  — see *Month-end reconciliation* below. `GET /lss/reconciliation/peek` returns the same
  report but requires the `X-Internal-Service` secret and is a legacy caller, not the
  interface.

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

### Month-end reconciliation

Compares the `payments` table (the capture side) against the processor settlement file
row by row, in integer minor units. Read-only — it issues `SELECT` only and never corrects
a balance.

```bash
docker compose exec servicing-service python -m app.reconcile
docker compose exec servicing-service python -m app.reconcile --from 2026-06-01 --to 2026-06-30
```

Without `--from`/`--to` the window is the settlement file's own date range. The JSON report
goes to **stdout** (pipe it), the human summary to **stderr** (read it).

**Requires `DUPLICATE_SUSPECT_WINDOW_SECONDS`** (seconds; `120` in `.env.example`). It has no
default and the job exits `2` without it, rather than scan for double charges against a
guessed bound.

**Requires `RECONCILIATION_ALERT_THRESHOLD_MINOR`** (minor units; `500` in `.env.example`,
which is the $5.00 the client set on 2026-08-14). Also no default, also exit `2` without it.

**The alert.** One alert, and it fires when the window's **per-loan absolute variance exceeds
the threshold**. Not the net — the net cancels opposite-signed breaks, and on the sample file
that is −88882 against 175318 absolute, so netting hides roughly half the error. Not the gross
break value — that moves with the matching tolerance (175318 at ±1 day, 257418 at ±0), so a
threshold measured against it would shift whenever someone tuned a constant. `DUPLICATE_SUSPECT`
does not feed it: a duplicate carries no variance, and counting it would count the same money
twice. The report states the threshold beside the result, so a reader never has to look up the
deploy's environment to interpret it.

**Individual breaks are listed whatever their size.** The threshold gates the alert only. A
sub-threshold break still appears in the exception output — the client asked for that
explicitly.

**Expect it to fire on the current data.** Per-loan absolute variance on the seeded sample is
175318 minor against a 500 threshold — roughly 350×, and loan 4471 alone is 50000, or 100×.
That is the open exception for PR-100290 / PR-100311 showing up in the alert exactly as
intended, not a mis-set threshold.

A daily close is this same job run with `--from` and `--to` set to the same date. Nothing in
this stack schedules it; the crontab line below is the operator's.

**Exit codes — a cron must not treat these as pass/fail.**

| Code | Meaning | Operator action |
|---|---|---|
| `0` | Reconciled, nothing found, variance within the threshold | None |
| `1` | Reconciled, breaks found **or the alert fired** | Work the break list below |
| `2` | **ABORT** — could not run the comparison | Fix the cause and re-run. **Not** a clean result |

`2` means the settlement file was absent, unreadable, empty, missing a column, or held a row
that would not parse — so nothing was verified. A cron that treats non-zero as one condition,
or that only alerts on `1`, turns "could not check" into silence.

```cron
# 06:00 on the 1st. Exit 1 (breaks) and exit 2 (abort) both need a human, for different reasons.
0 6 1 * * cd /srv/meridian && docker compose exec -T servicing-service python -m app.reconcile > /var/log/meridian/recon-$(date +\%Y\%m).json 2>> /var/log/meridian/recon.log || echo "reconciliation exit $? — see recon.log" | mail -s "Meridian month-end" ops@example.com
```

**Break classes.**

| Class | Meaning | What to do |
|---|---|---|
| `MISSING_IN_LEDGER` | Settled capture with no `payments` row — money taken, never credited | Customer-affecting. **Finance Ops owns these** (see **Ownership** below). Trace the `processor_ref`, credit the loan through the normal apply path |
| `MISSING_IN_SETTLEMENT` | `payments` row with no settled capture — credited, never captured | Check whether the capture failed after the row was written; the balance may be understated |
| `REFUND_UNREPRESENTED` | Settlement refund the `payments` table cannot hold (no direction column) | Expected today; a schema limitation, not a lost payment. Note it and move on |
| `AMOUNT_MISMATCH` | Same loan and window, different amount | Compare the two figures in the report's `detail`; usually a partial capture |
| `DUPLICATE_SUSPECT` | Two `payments` rows, same loan and amount, inside the gap bound | Suspected double charge. The entry names both rows — `first_payment_id` and `second_payment_id` — and those are the ids to act on; the summary prints them as `payments <first>,<second>`. **Reported separately from the breaks and never added to a variance figure** — the money is already counted on whichever side it landed |

**The three figures are not interchangeable.** The report labels each with whether it moves
with the matching tolerance:

- **Net variance** — ledger total minus settlement net. Does not depend on the tolerance.
- **Per-loan absolute variance** — the sum of each loan's variance, unsigned. Does not depend
  on the tolerance. Where this and the net differ, the difference is error the net is hiding.
- **Gross break value** — the sum of all break amounts. **Depends on the tolerance**, so it is
  partly an artifact of a constant we chose, not purely a measurement.

Quoting only the net is what produced "month-end is a little noisy": on the sample data it
reads −88882 against a per-loan absolute of 175318, so netting hides roughly half the error.

**Ownership.** **Finance Ops owns `MISSING_IN_LEDGER` breaks** — the client's decision of
2026-08-14, given when asked who owned the $500 on loan 4471: treat it as an open exception, not
a write-off, put both processor references in the first exception report, and mark them for
Finance Ops review.

Two limits of that decision, stated so nobody reads more into the report than it carries:

- **Marking is what this report does. It is not a workflow.** The client ruled out anything
  larger — no owner column, no status field, no ticket integration, no remediation path. A break
  appears in the report with its `processor_ref`; a human reads it. There is nothing here that
  tracks whether they did.
- **Not a write-off means the money stays visible.** The alert keeps firing on an unresolved
  break every run, by design. Loan 4471 alone is 50000 minor against a 500 threshold, so it will
  fire until the underlying exception is actually worked. Do not raise the threshold to quiet it.

**No recipient is configured.** The cron example above mails `ops@example.com`, which is a
placeholder, not an address anyone reads. Who receives the alert, at what time, through what
channel was asked on 2026-08-12 and has not been answered — the reply that came back on 08-14
settled the threshold and the cut-off, and that question was bundled in the same row as the
cut-off, so it is easy to read as answered. **Until a real recipient is set, this control detects
and reports to nobody.** Substitute a distribution list before wiring the cron, and do not
present the alert as covered until then.

`DUPLICATE_SUSPECT` has no equivalent owner. The client's 08-14 answer was about
`MISSING_IN_LEDGER`; what a double-charged borrower is told, whether a refund is submitted, and
who owns that refund is an open question and not a gap in this runbook.

## Known operational pain (unresolved)

- **Self-decision guard can't see a pre-migration or anonymous self-submit (D24).**
  `deny_self_decision` blocks an officer from deciding their own application when either
  `users.applicant_id` links their account to it, or `applications.submitted_by_user_id`
  (captured at submit from `X-User-Id`, migration 0017) matches their user id. Neither
  check can see an application submitted anonymously — including every row from before
  this migration, which has no submitter recorded at all and is indistinguishable from a
  genuine anonymous applicant. No automated gate can close this without blocking the
  ordinary anonymous-apply flow permanently. Manual back-book check: for any application
  an officer decisioned with no linked account, cross-reference the applicant's recorded
  name against staff `display_name` by hand. This lists the candidates:

  ```sql
  SELECT a.id, ap.name, ap.created_at
  FROM applications a JOIN applicants ap ON ap.id = a.applicant_id
  WHERE a.submitted_by_user_id IS NULL
  ORDER BY a.created_at;
  ```

  See `docs/debt-log.md` D24.
- **Payment retries.** The processor occasionally times out; clients retry. `payment-service`
  has no idempotency key, so retried payments insert a second row and apply twice (the second
  `apply-payment` call posts again). We field "double charge" support tickets a few times a
  month. (No fix yet — moved with the code into `payment-service`.)
- **Decision/disclosure/KYC stalls block applicants.** Origination calls these over
  synchronous HTTP with no timeout or retry. If `decision-service`'s credit pull hangs, the
  applicant-facing origination request hangs with it. Watch `decision-service` latency when
  intake requests pile up. (No circuit breaker / fallback.)
- **Month-end reconciliation is schedulable, not scheduled.** The comparison itself now
  exists (see *Month-end reconciliation* under Common tasks), but this stack runs no
  scheduler and this work deliberately did not add one — nothing triggers the job until an
  operator wires the `cron` line below. A scheduler that silently stops is the same defect
  class as a comparison that silently reads nothing.
- **A manual balance adjustment can still conceal a discrepancy.** Reconciliation compares
  captures to settlement; it never reads `balances`. `adjust-balance` and `waive-fee` move
  money without producing a `payments` row, so they create no break — and a representative
  who adjusts a balance until it "looks right" changes nothing this job reads. Detecting
  that needs the actor/before/after record the week-6 ledger work specifies. The two
  controls are complementary; neither substitutes for the other.
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
