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
- Postgres: **not published to the host** (D21a). Open a session inside the network with
  `docker compose exec postgres psql -U meridian -d meridian` — the credential is in `.env`.
- Redis: **not published to the host** (D21b). Use `docker compose exec redis redis-cli`. It
  runs with no `requirepass`, and it holds live session tokens and raw resume-continuation
  tokens, so treat anything that reaches it as holding officer credentials.
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

### Purging the stored CVV (migration 0020, D13a)

Retaining sensitive authentication data after authorization is a flat PCI-DSS 3.2.1
prohibition, so this migration deletes the values and the column rather than stopping the
writes. Both charge handlers stopped writing it in the same change, and **both services
refuse to serve while the column is still present**: their readiness probe reports
`schema_not_ready:payments.cvv_present`, `/health` is unhealthy and a charge returns 503.
That is deliberate — the alternative is serving over a schema holding prohibited data.

```bash
docker compose exec -T postgres psql -U meridian -d meridian \
  -f - < db/migrations/0020_payments_drop_cvv.sql
```

Two operational facts before you run it:

- **It takes an ACCESS EXCLUSIVE lock.** The final `VACUUM FULL payments` is what actually
  destroys the old row versions; while it runs, `payments` is unreadable and unwritable,
  so charges fail for the duration. Run it in a maintenance window, or use
  `pg_repack -t payments` instead of that statement, which reaches the same place online.
- **Do not wrap the file in a transaction.** `VACUUM` cannot run inside a transaction
  block, so `psql -1` or a surrounding `BEGIN` makes it fail.

Verify, in this order — the column being gone does not prove the values are:

```bash
# 1. the column is gone (this is what the readiness rung checks)
docker compose exec -T postgres psql -U meridian -d meridian -c \
  "SELECT column_name FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'payments' AND column_name = 'cvv';"   # expect 0 rows

# 2. both services came back healthy. Every backend service is `expose:`-only in
#    docker-compose.yml — the gateway on :8000 is the sole host-published trust
#    boundary — so a bare `curl localhost:8006/health` from the host is refused.
docker compose exec -T payment-service   python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8006/health').read())"
docker compose exec -T servicing-service python -c \
  "import urllib.request; print(urllib.request.urlopen('http://localhost:8002/health').read())"
```

**What this does not purge, and who owns it:** WAL segments, replicas, and any backup
taken before the rewrite still contain the CVV values. That is a retention action on the
operator side, not a schema change — `docs/debt-log.md` D13 tracks it under "Not covered".
The PAN column is untouched and stays open as D13b.

### Purging stored SSNs (D33) — NOT YET RUNNABLE

`services/origination-service/app/purge_ssn.py` exists and **must not be enabled**. This
section is here because that module points at it; it is a statement of what is missing,
not a procedure to follow. Read it before anyone asks you to "just turn the purge on".

Three gates stand between the file and a purged row, and **all three are still shut**:

| Gate | State | Who opens it |
|---|---|---|
| `SSN_PURGE_ENABLED` env var | unset | operator, once the retention answer lands |
| `--execute` CLI flag | not passed | operator, per run |
| `_ELIGIBILITY_IS_PLACEHOLDER` module constant | `True` | **a reviewed code change, not an operator** |

The third gate is the one that matters today. The module's `WHERE` clause purges on
**calendar age since submission**, which is wrong: the bureau pull needs the real digits
while an application is still decisionable, so the trigger has to be every application
tied to that applicant reaching a terminal state. Running the current query would null
`applicants.ssn` for live applications and break their re-pull, irreversibly. With the
constant set, `--execute` aborts with exit 2 instead.

Dry-running is safe and is the only supported operation right now:

```bash
docker compose exec -T origination-service \
  env SSN_PURGE_WINDOW_DAYS=365 python -m app.purge_ssn
```

Exit codes match `app/reconcile.py`: **0** ran (dry or executed), **2** refused to run
(placeholder eligibility, unset/invalid window, or a DB error). A refusal is never a
clean 0.

**Before this can ever run, three things must land, in order:**

1. The client's retention answer — how long the platform must be able to re-run a bureau
   pull. `docs/debt-log.md` D33 and the GLBA handoff carry the question.
2. The eligibility query rewritten to an `applications`-status join, and
   `_ELIGIBILITY_IS_PLACEHOLDER` cleared **in the same change**.
3. An answer on dead tuples. **This is where the CVV procedure above does not transfer.**
   Migration 0020 dropped a column once and could rewrite the table with a single
   `VACUUM FULL`. This purge is recurring and per-applicant, and `VACUUM FULL` takes an
   `ACCESS EXCLUSIVE` lock on `applicants` — it would block intake and every officer read,
   so it cannot run after each purge. Until someone establishes that routine autovacuum
   reclaims those row versions on an acceptable schedule, or schedules maintenance
   windows for it, "purged" means *nulled in the live row and still readable in the data
   files* for an unbounded period. Do not report a purge as complete on the strength of
   the `UPDATE` alone.

**What this will not purge, and who owns it:** the same list as the CVV section above, and
for the same reason — pre-redaction log files, WAL segments, replicas, and any backup.
Nulling the column does not remove the SSN from the system. `docs/debt-log.md` D33 tracks
these under "Not covered"; they are operator retention actions, not schema changes.

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

### Reading the officer assistant's run telemetry

Every officer assistant request writes one `assistant_runs` row at the entry span
(migration 0021). `GET /assistant/metrics` on origination-service is the only way those
rows are meant to leave the database: an officer-gated aggregate of counts and enum codes.

```bash
curl -s -H 'X-User-Role: underwriter' 'http://localhost:8001/assistant/metrics?window=7 days'
```

`window` is one of `1 day`, `7 days`, `30 days`. Anything else is a `422` listing the
three — it is never silently defaulted, because a fall back would answer a different
question than the one asked.

What the response means:

- `recorded_runs` is the denominator, and it is **recorded runs, not requests**.
  `assistant_runs.record` swallows every write failure by design, and a refusal raised
  above the assistant loop (including this endpoint's own `403`) is never recorded at all.
  So `refusal_rate_among_recorded_runs` is a rate among rows that exist, not among
  officer requests that happened.
- `refusal_rate_among_recorded_runs` is `null`, never `0.0`, when nothing was recorded. A
  zero rate over a zero denominator is a claim about a population nobody observed.
- `truncated: true` means the window held more distinct groups than the endpoint serves,
  so every count in that response — `recorded_runs` included — describes the largest
  groups and not the whole window.
- `unrecognised` in `task`, `refusal_code`, `outcome` or `policy_band` means a row held a
  value outside the vocabulary this service knows. `outcome` and `policy_band` carry no
  CHECK constraint, so the endpoint masks rather than serves it. Treat it as a signal that
  something wrote to the table outside the normal path — a hand-applied fix, a backfill, a
  restored volume — and go look at the row.

Failure modes, and how to tell them apart:

- **`503 database not ready for assistant metrics (schema_not_ready:assistant_runs...)`** —
  the volume never had migration 0021 applied. Apply it (`db/migrations/0021_assistant_runs.sql`)
  and re-check `/health`. This is also the state in which the write path has been silently
  recording nothing: `record()` swallows, so there is no other symptom.
- **An empty aggregate with a `200`** — either a quiet week or a telemetry fault. The two
  are not distinguishable from this endpoint, by design; `/health` answers whether the
  table and its constraints are there.
- **`500 internal error`** — the log line names the model and the field names that
  disagreed with the SELECT, never the row's values.

The rows themselves keep `application_id` and `trace_id` and have **no retention policy**
(`docs/debt-log.md` D5 carries the rotation/retention row). This endpoint is the export
boundary; direct SQL against the table is not, and puts an applicant-linkable identifier
in whatever consumes it.

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
- **Payment retries.** The processor occasionally times out; clients retry. An exact retry
  under the same `Idempotency-Key` is now prevented at capture — `payment-service` and
  `servicing-service` both claim the key insert-first against a partial unique index before
  the processor is contacted (D19, PR #63 schema + PR #65 claim path), held by the blocking
  `payment-idempotency-gate`. Still not prevented: a processor-side duplicate, or any break
  from before the fix — reconciliation is what catches those, not this control. See
  `docs/debt-log.md` D19.
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
- **Redaction ships; the log files written before it do not.** New log lines are redacted:
  `PiiRedactor` (ADR 0006) merged in PR #2 (`1f89ac1`) and runs in all 7 services, held
  identical by the blocking `redactor-drift` job and covered by the blocking
  `redaction-tests` job, and origination's intake logs an allowlist of fields rather than
  the payload, so the redactor is a backstop there rather than the only control. What is
  still unsafe is history: any `logs/payment-service.log` or `logs/origination-service.log`
  written before that merge still holds plaintext PAN/CVV/SSN, and nothing has audited or
  deleted them. Do not ship pre-redaction log files to a third-party aggregator, and do not
  assume rotation has trimmed them — no rotating handler is configured, so D5's
  retention/audit rows are still open.
- **Secrets are purged from the tree, not rotated.** The committed `.env` and the hardcoded
  `config.py` fallbacks are gone from `main` — PR #4 (`ed2cb35`, 2026-07-10) — and the
  blocking `secret-scan` job fails on the literals and on a tracked `.env`; every key now
  reads `os.getenv(..., "")` with no committed default. **The keys themselves are still live
  and still owed a rotation:** the Experian and core-banking keys and the `payment-service`
  processor key remain in git history and wherever they were already used, so a purge
  removed them from the tree and nothing more. Rotate before any real go-live (D1, open).

## Backup and recovery

**There is no backup or recovery procedure. This section exists to say so, not to describe one.**
Week-8 client-demo feedback recorded this as a baseline to capture; the honest baseline is that no
value exists to report. Nothing below is a target, an RPO, or an RTO — none has been agreed.

**What exists.** One Docker named volume, `pgdata`, mounted at `/var/lib/postgresql/data`
(`docker-compose.yml:13`, declared at `:227`). `make seed` re-applies `db/init/002_seed.sql`, which
restores *demo* rows and nothing a borrower ever touched. That is the whole of it.

**What does not exist**, verified by search rather than assumed — the repo contains no `pg_dump`,
`pg_basebackup`, or `pgbackrest` invocation in any script, Makefile target, or compose file, and no
file whose name mentions backup or restore:

- No scheduled dump, no WAL archiving, no replica.
- No restore procedure and no restore drill, so recovery time is unmeasured, not merely unstated.
- No retention or destruction policy for whatever backups the client already holds.
- No encryption-at-rest statement for backup media.

**Why this is a control question and not only an operations one.** `docs/debt-log.md` D13 records
that WAL segments, replicas, and any backup taken before a card-data rewrite still contain
cardholder data, and that historical card tokens are not recoverable — re-tokenizing the back book
is its own migration. D5's mitigation path carries an explicit "audit all existing backups;
re-encrypt or delete any containing plaintext PII" row, and **that row is open**: the redactor
stops new plaintext PAN/CVV/SSN reaching logs, but it does nothing to files written before it
merged, or to a database backup where the PAN is a stored column rather than a log line. So a
restore is currently also a re-introduction of the exact data two blocking CI jobs exist to keep
out.

**Open, and owned by the client.** Who takes backups of the production database today, on what
schedule, where they are stored, and whether any predate the card-data work. Until Lending Ops
answers, this platform can state only what it does itself, which is nothing. Do not present a
backup posture in a demo — say this section's first line instead.

## Tests

```bash
make test    # runs pytest in both backend services (non-blocking)
```

Some money-math tests (`test_apr.py`, `test_money.py`) currently FAIL by design — they
encode the float-rounding defects we have not fixed. CI runs them `continue-on-error`.
