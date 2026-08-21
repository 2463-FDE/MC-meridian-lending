# Meridian Lending — Debt Log

**Date:** 2026-07-01  
**Scope:** Known security, compliance, and architectural debt

This document tracks known issues, their business/compliance impact, and mitigation paths. It is not an exhaustive audit; it captures *known, documented* debt discovered during the LLM infrastructure build (Week 1).

---

## Debt Entries

### D1: Hardcoded Credentials in Code and Environment

| Field | Value |
|---|---|
| **ID** | D1 |
| **Finding** | Bureau and payment-processor API keys are hardcoded in source code and `.env`. |
| **Location** | `services/decision-service/app/config.py`: hardcoded `EXPERIAN_KEY` inline default (value redacted; lines ~15–20) |
| | `services/origination-service/app/config.py`: Stale duplicate (lines ~18–22) |
| | Root `.env` (committed): hardcoded `CORE_BANKING_API_KEY` and card-processor key (values redacted; line ~12–14) |
| | *(Literal values intentionally not reproduced here — see `docs/security-remediation-2026-07.md`. They are purged from source on `main` — PR #4 (`ed2cb35`, 2026-07-10) — and the blocking `secret-scan` job fails on the literals and on a tracked `.env`. **They must still be rotated:** a purge removes them from the tree, not from wherever they were already used.)* |
| **Risk** | **Critical.** If repo is leaked (GitHub public, compromised dev machine, etc.), all live credentials are exposed. Attacker can: make credit pulls against Experian, charge cards, access banking APIs. PCI-DSS violation (3.5.1: no hardcoded secrets). |
| **Current Impact** | Keys are in source control history (git log). Even if deleted now, they remain in old commits. |
| **Mitigation Path** | **Week 2+:** Rotate all compromised keys immediately. Move credentials to sealed env vars or secret manager (e.g., AWS Secrets Manager, HashiCorp Vault). Use CI/CD to inject secrets at deploy time. Remove all old commits containing keys (force-push after rotation, or rewrite history). |
| **Status** | Open; flagged as debt; no immediate fix (out of scope for Week 1). |

---

### D2: Float Arithmetic for Money

| Field | Value |
|---|---|
| **ID** | D2 |
| **Finding** | All monetary amounts are stored and calculated as `DOUBLE PRECISION` (float), not fixed-point decimal. |
| **Location** | `db/init/001_schema.sql` (lines 33, 36, 68–72, 80–81, 90, 101): `DOUBLE PRECISION` used for `amount`, `income`, `apr`, `monthly_payment`, `finance_charge`, `balance`, etc. |
| | `services/servicing-service/app/balance.py`: `new_balance = current - float(amount)` (line ~25) |
| | `services/disclosure-service/app/apr.py`, `fees.py`, `offer.py`: All calculations use float. |
| **Risk** | **High.** Rounding errors compound across calculations. Example: |
| | Loan: $10,000 / 36 months = $277.7777... per month. |
| | Each month, 2–4 cents of rounding error. |
| | After 36 months: balance may not be exactly $0. |
| | Impact: Reconciliation fails, audit logs show discrepancies, customer complaints ("why is $0.03 still owed?"). |
| | PCI-DSS **does not prohibit** float math, but it creates operational risk (disputes, chargebacks). |
| **Current Impact** | Test suite (`services/*/tests/test_money.py`) includes tests that **fail by design**; they document rounding defects. No one reacts to these failures (CI runs with `|| true`). |
| **Mitigation Path** | **Week 2–3:** Migrate to `NUMERIC(19,2)` (fixed-point, cents precision) in DB. Update all ORM models to use `decimal.Decimal`. Recalculate all outstanding balances post-migration (audit + customer communication). Add pre-payment validation to round amounts to cents. Tighten test suite to fail on rounding discrepancies > $0.01. |
| **Week 4 progress** | **Partially mitigated — the disclosure compute path only (ADR 0012).** `disclosure-service` `apr.py` / `schedule.py` / `offer.py` are `Decimal` end to end, and the new `disclosures` table holds money as integer **minor units** with the APR as exact `NUMERIC(9,3)`. That row is the authoritative TILA record; the float `offers` columns are now a rounded convenience copy, and where the two disagree `disclosures` wins. The path is guarded by the **blocking** `tila-vectors-gate` — the only money math in this repo that cannot regress under `\|\| true`. Chosen as the beachhead because it is where float error is a *regulatory* defect rather than a reconciliation nuisance. |
| **Still open** | Intake, decisioning, servicing, and payments remain float throughout, as does `balances` (a single mutable column, no ledger). The `offers` money columns were deliberately **not** converted: rewriting the figure disclosed to a borrower would destroy the evidence of what was disclosed (see `db/migrations/0012_disclosures.sql`). Converting those paths needs its own migration and a balance-recalculation plan. |
| **Status** | **Partially mitigated (disclosure compute path, Week 4, ADR 0012); open everywhere else.** |

---

### D3: Unlocked read-modify-write on `balances` — concurrent applies lose money

*(Entry created 2026-08-02, Week 5. **D3 was already cited four times in service code** —
`servicing-service/app/balance.py:5`, `app/main.py:83`, and the two payment docstrings — but
had no entry in this log. It has one now, with a measurement.)*

| Field | Value |
|---|---|
| **ID** | D3 |
| **Finding** | `balance.apply_payment` reads the balance, computes the new value in Python, then writes it back — no lock, no transaction around the pair. Concurrent applies to the same loan read the same opening value and overwrite one another, so captured money never reaches the loan. |
| **Location** | `services/servicing-service/app/balance.py:23-31`; the three steps are labelled `# READ`, `# MODIFY`, `# WRITE` in the source. Reached via `app/main.py:80-85` (`apply-payment`, the endpoint `payment-service` calls) and `app/payments.py:79`. |
| **Trigger** | Any two concurrent balance mutations on one loan — two payments, or a payment and a fee waiver (the case the `balance.py` docstring already names). |
| **Measured** | **2026-08-02, `scripts/repro_double_charge.py` against the live stack.** Loan 4471, one $100.00 intent sent 8 ways concurrently: 8 `payments` rows, **$800.00 captured, $600.00 credited** — $200.00 taken and never applied. Non-deterministic (a 5-way run lost one application, the 8-way run lost two). Every request returned `200`. |
| **Risk** | **High.** Money is captured and not credited: the borrower is charged and still owes the amount. `reconciliation.py:14` sums the `payments` table so the aggregate gap is arithmetically visible, but with no ledger (D2) nothing can attribute it to a customer afterwards — part of why the "charged twice" tickets were dismissible as confusion. |
| **Attribution** | **Pre-existing** (baseline servicing). Named in the `balance.py` docstring since the baseline; never entered in this log and never measured until Week 5. |
| **Mitigation Path** | Make the mutation a single atomic statement — `UPDATE balances SET balance = balance - :amount WHERE loan_id = :id` — committed in the same transaction as an append-only `payment_applications` row unique on `payment_id`. Specified as **D3d** in `docs/spec-payments-week5.md`; decided in **ADR 0013**. Distinct from the idempotency key: the key collapses duplicate *intents*, but two genuinely distinct concurrent payments still race. |
| **Status** | Open. **Fix specified (ADR 0013, Week 5); not built** — spec-only week. |

**Bookkeeping note:** the D-numbers used in service code and the ones defined in this log
have drifted. Code cites `D3`, `D4`, `D7`, `D11`, `D12`, and `D14`; this log defines `D14`
as *"Encoded PII bypasses log redaction"* while `servicing-service/app/main.py:83` uses
`D14` for *"no payment waterfall"*. D3 is reconciled here. The remaining collisions are left
alone — renumbering live code comments is a separate mechanical change.

---

### D5: Plaintext PAN/CVV/SSN in Logs

| Field | Value |
|---|---|
| **ID** | D5 |
| **Finding** | Payment and origination services log full request/response bodies, including plaintext PAN, CVV, and SSN. |
| **Location** | `services/payment-service/app/logging_config.py` (lines 1–4): docstring — "writes the full charge request body (PAN, CVV, SSN) at INFO. No redaction." |
| | `services/payment-service/app/payments.py` (lines 23–27): `charge()` logs the full request body `{"pan","cvv","ssn","amount","loan_id","name"}` at INFO on `POST /payments`. |
| | `services/origination-service/app/logging_config.py` (line 3–4): "Logs the full request body on every POST — including PII. No redaction." |
| | `services/origination-service/app/intake.py` (line 15): `log.info("POST /applications intake req=%s", payload)` — payload includes SSN, email, phone. |
| | **Log files:** `logs/payment-service.log`, `logs/origination-service.log` contain unredacted cardholder data. |
| | **Sample from repo handover:** `INFO charge req={"pan":"4111111111111111","cvv":"123","ssn":"412-55-9981","amount":250.00}` |
| **Risk** | **Critical.** PCI-DSS 3.4: "Rendering PAN unreadable anywhere it is stored (including on portable digital media, backup media, and **in logs**)." |
| | If log files are: |
| | - Backed up to S3/tape (unencrypted or with lost key), PII is exposed. |
| | - Aggregated to a central logging service (Loki, ELK, Splunk) without redaction, PII is searchable. |
| | - Left on disk after server failure, physical recovery exposes PII. |
| | Violation triggers: fines (up to $100k+ per incident under state laws), customer breach notifications, reputational damage. |
| **Current Impact** | Logs are actively created and written to disk daily. No retention/rotation policy documented. If server is decommissioned, logs may be left in place. |
| **Mitigation Path** | **Week 1 (NOW):** Implement `PiiRedactor` class; apply to all 7 services' logging (ADR 0006). Redact PAN, CVV, full SSN, email, phone before writing to disk. Preserve last 4 of SSN for audit trails. |
| | **Week 1 (ongoing):** Flag existing log files (in this debt-log). Do not delete; archive separately (out of scope). |
| | **Week 2:** Implement log rotation + deletion (30-day retention). |
| | **Week 2:** Implement centralized logging (Loki/ELK) with redaction at ingest. |
| | **Week 3:** Audit all existing backups; re-encrypt or delete any containing plaintext PII. |
| **Status** | **Mitigated** — the Week-1 redaction row is done and merged; the Week-2/Week-3 rows are not. `PiiRedactor` (ADR 0006) merged in PR #2 (`1f89ac1`) and now ships as `services/*/app/redactor.py` in all 7 services, kept identical by the blocking `redactor-drift` job and covered by six suites (`test_logging_redaction.py` in kyc/origination/payment/servicing, plus origination's `test_redactor.py` and `test_pii_matrix.py`) under the blocking `redaction-tests` job. `intake.py` also logs an allowlist of fields rather than the payload, so the redactor is a backstop there, not the only control. **Not closed.** Open residuals: encoded PII still bypasses the redactor (D22 closed the raw-separator gaps; D14 covers percent- and unicode-encoded PII and stays deferred); no log rotation or 30-day retention exists (`logging_config.py` configures no rotating handler); no centralized logging with redaction at ingest; the pre-redaction log files and any backups containing them are flagged here but not audited. |

---

### D13: PAN and CVV Stored in Database

| Field | Value |
|---|---|
| **ID** | D13 |
| **Finding** | Full PAN and CVV are stored in plaintext in the `payments` table. |
| **Location** | `db/init/001_schema.sql` (lines 96–105): |
| | ```sql |
| | CREATE TABLE IF NOT EXISTS payments ( |
| |     id          SERIAL PRIMARY KEY, |
| |     loan_id     INTEGER REFERENCES loans(id), |
| |     pan         TEXT,                 -- full PAN stored |
| |     cvv         TEXT,                 -- CVV stored (SAD — flat PCI prohibition) |
| |     amount      DOUBLE PRECISION NOT NULL, |
| |     method      TEXT DEFAULT 'card', |
| |     created_at  TIMESTAMPTZ DEFAULT now() |
| | ); |
| | ``` |
| | **Rationale (from ADR 0003):** "Customer support wants to 'see the card on file' when a borrower calls about a payment, and finance wants to re-run a charge without asking the customer for the number again." |
| **Risk** | **Critical.** PCI-DSS 2.1, 3.2.1: "Do not store PAN, CVV, or CVC after authorization." |
| | If Postgres is breached (e.g., SQL injection, ransomware, stolen backups), all historical card data is exposed. |
| | Attacker can: reuse stolen cards, commit fraud in customer's name, sell card data. |
| | Liability: PCI-DSS fine ($5,000–$100,000 per month until remediated), potential state AG fines (up to $1,000 per customer per month under some state laws). |
| **Current Impact** | Every payment since go-live is stored with full PAN/CVV. Unclear how many customers/cards are in the table (row count not given). |
| **Mitigation Path** | **NOT Week 1.** This is structural debt requiring: |
| | 1. PCI-DSS-compliant tokenization (e.g., Stripe, AWS Payment Cryptography, or self-hosted HSM). |
| | 2. Modify `payments` table: replace `pan`, `cvv` with `token` (opaque reference to tokenized card). |
| | 3. Re-tokenize all historical data (data migration, potential customer re-auth for PCI audit). |
| | 4. Update charge logic to use tokenized card. |
| | **Week 2–3 candidate** if board prioritizes PCI compliance. Otherwise, deferred to Q2. |
| **Week 5 design** | Specified in `docs/spec-payments-week5.md` D4 and decided in **ADR 0013**, which **supersedes ADR 0003**. Three points matter. (1) The CVV column is *dropped and purged*, not merely unwritten — retaining sensitive authentication data after authorization is a flat prohibition, so the remediation is a deletion. (2) The purge migration must **rewrite the table** (`VACUUM FULL`, or `pg_repack` to avoid downtime): Postgres `DROP COLUMN` only marks the attribute dropped and the preceding `UPDATE ... SET pan = NULL` leaves the old row versions as dead tuples, so without a rewrite every PAN is still recoverable from the data files. (3) Browser-side tokenization means the PAN never reaches a Meridian server, so the self-serve form *shrinks* assessment scope instead of growing it. Retained instead: `card_token`, `card_brand`, `card_last4`, expiry. Held by a new blocking CI job, `no-sad-gate`. |
| **Not covered** | WAL segments, replicas, and backups taken before the rewrite still contain cardholder data; that needs its own retention action. Historical card *tokens* are not recoverable — re-tokenizing the back book is a separate migration. |
| **Status** | Open. **Fix specified (ADR 0013, Week 5); not built** — spec-only week. Will block production deployment until addressed. |

---

### D17: Offer-replay schedule uses a divergent APR default (0 vs 7.99)

| Field | Value |
|---|---|
| **ID** | D17 |
| **Finding** | `_offer_response_from_persisted` defaults a null persisted APR two different ways in one response: the disclosure box uses `row["apr"] or 0`, but the display amortization schedule uses `row["apr"] or 7.99`. A null-APR offer row would be disclosed as 0% APR alongside a schedule computed at 7.99%. |
| **Location** | `services/disclosure-service/app/routers/offers.py` (`_offer_response_from_persisted`, lines ~54 and ~66). |
| **Risk** | **Low.** Not reachable today: `accept_offer` (origination) rejects a null-APR offer before boarding, and `build_offer` always writes a non-null APR. Cosmetic inconsistency copied from the pre-existing GET read path; would only surface on a hand-corrupted/legacy null-APR row. |
| **Current Impact** | None observed. Defensive-default mismatch only. |
| **Mitigation Path** | Pick one default for both fields (0), or drop the schedule's `7.99` magic fallback and render an empty schedule when APR is null. Trivial, deferred until the offer read/replay path is next touched. |
| **Status** | Open; accepted residual (display-only, not reachable). |

---

### D18: Fresh-insert vs replay offer schedule can differ by a cent

| Field | Value |
|---|---|
| **ID** | D18 |
| **Finding** | The first (fresh-insert) `POST /offers` returns the true `amortization(body.principal, …)` schedule; an idempotent retry returns a schedule reconstructed from the stored disclosure box (principal backed out via `amount_financed / (1 - 0.03)`, term via `round(total/monthly)`). Float round-trip is exact at tested values but can drift a cent on individual schedule rows. |
| **Location** | `services/disclosure-service/app/routers/offers.py` (`_offer_response_from_persisted` reconstruction vs the fresh-insert branch of `create_offer`). |
| **Risk** | **Low.** Display schedule only — the disclosure box (APR/finance charge/payment/total the borrower accepts and `accept_offer` boards) comes straight from the persisted row and is byte-identical across first call and replay. Same float family as D2. |
| **Current Impact** | Per-row cent drift possible between an original response and its retry; regulated totals unaffected. |
| **Mitigation Path** | Subsumed by D2 (Decimal money migration): once principal/term are stored (or money is fixed-point), the back-out reconstruction goes away and both paths render the identical schedule. No standalone fix. |
| **Status** | Open; accepted residual (tracked under D2). |

---

## Teeth review 2026-07-19 — attribution + new entries

Adversarial full-branch review of `main` @ `2ecdb27`. **Attribution:** every Critical/High
finding is **pre-existing brownfield** (baseline `e8bb2fa`/`d59f331`/`60d1c37` or the 7-service
decompose `4c464b8`), in the servicing/payment/schema layers our features never touched. The one
finding in code our features introduced (redactor separator blind spots, Week-1 PII work
`73ef737`) was **fixed** and is on `main` — PR #9, `170ed29` — see D22. Everything else is
logged here as pre-existing debt; not fixed, out of scope for "parts we touched".

### D8: Servicing service enforces NO authorization (IDOR + no maker-checker)

| Field | Value |
|---|---|
| **ID** | D8 (referenced in `servicing-service/app/main.py` comments; entry created 2026-07-19) |
| **Finding** | *(As found 2026-07-19, and no longer true of `main` — see Status.)* `servicing-service` has no authz module at all. Gateway `/lss/*` and `/payments` proxies do session-auth only (no role check). Two consequences: **(a) IDOR** — any authenticated user reads or mutates any loan by walking serial ids; **(b) no role/maker-checker** — a borrower can move money on any account. |
| **Location** | Reads: `servicing-service/app/routers/loans.py` (`GET /loans/{id}` L55–66, `GET /loans/{id}/payments` L78–91), `main.py` (`GET /accounts/{id}/balance` L88–94). Mutations: `main.py` `adjust-balance` L101–105, `waive-fee` L112–116, `apply-payment` L79–85. Gateway: `main.py` `/lss` L421–425, `/payments` L457–464. |
| **Trigger** | Log in as borrower `maria`; `GET /lss/loans/{1,2,3…}` enumerates every borrower's loan/balance/payment history; `POST /lss/accounts/1/adjust-balance {"new_balance":0}` zeroes a stranger's balance; `.../waive-fee` waives any fee. All succeed. **None of these succeeds on `main` today** — each route now refuses before it reads or writes. Kept as the reproduction that produced the entry. |
| **Risk** | **Critical.** Cross-customer PII disclosure + unauthenticated-in-effect money mutation. |
| **Attribution** | **Pre-existing** (baseline `d59f331` servicing). Related to **ADR 0010** (officer-or-owner authz), which we implemented on origination only and explicitly deferred for servicing pending an applicant identity/signup flow that does not exist. Not our feature's code; the deferral is documented in ADR 0010. |
| **Mitigation Path** | Extend ADR-0010 `require_officer_or_owner` (plus a role gate + second-approver on money moves) to servicing. Needs the identity flow ADR 0010 is blocked on. Own PR/ADR. |
| **Week 5 split** | This entry conflates two problems with very different costs, and ADR 0013 separates them. **(a) `apply-payment` is an internal endpoint left publicly routed.** `servicing-service/app/main.py:80` reduces a balance, is reachable through the gateway on session auth alone, has no `X-Internal-Service` gate, and never checks that a payment was captured — so a caller can credit a balance with no card and no `payments` row. That is money creation, and it is **not an authorization model problem**: `kyc-service/app/routers/kyc.py`, `decision-service/app/routers/decisions.py`, and `disclosure-service/app/routers/offers.py` each already require the header. Specified as spec D3a — hours of work, no identity model needed. **(b) The rest is genuine RBAC** — ownership on reads, officer-only on `adjust-balance` / `waive-fee` — and stays deferred under ADR 0010. Self-serve payments do not require it, because the `pay:loan:{id}` capability token (spec D6) makes ownership an invariant of the credential rather than a per-request lookup. |
| **Status** | **Partially mitigated.** Both halves this entry named are closed on `main`; the maker-checker half is not. PR #32 (`f901894`, 2026-08-13) added `services/servicing-service/app/authz.py` — the module this entry says does not exist — and every route now guards before it reads or writes: `require_internal_caller` on `apply-payment` (`app/main.py:171`), `late-fee` (`:259`) and `/reconciliation/peek` (`:285`), which is (a); `require_money_role` on `adjust-balance` (`:228`) and `waive-fee` (`:245`), `require_money_role_or_owner` on `POST /payments` (`:87`), and `require_staff_or_owner` on the balance read (`:200-208`) and on the loan detail, schedule and payments reads (`app/routers/loans.py:119`, `:144`, `:164`), with `require_staff` on the list route (`:37`) — which is the RBAC half of (b). Ownership did not need ADR 0010's identity flow after all: the guard takes the owner from `X-User-Id` rather than from a signup flow, which is the ADR 0010 seed decision. **Open: the maker-checker half.** A single money role still both makes and approves a balance adjustment; no second approver exists. ADR 0017 (#40, `b6f1149`) proposes it and is Proposed, not built. **Also open:** the gateway still does not enforce role authz on money actions — it authenticates and forwards `X-User-Role`, so servicing is the only enforcement point (`services/gateway/app/main.py:7` says so). Not closed until maker-checker lands. |

### D19: Payment charge has no idempotency key (double-charge on retry)

| Field | Value |
|---|---|
| **ID** | D19 |
| **Finding** | The `payments` table has no idempotency key / unique charge reference, and `charge()` inserts a row then calls `apply_payment` with no dedupe. A retried/double-submitted `POST /payments` inserts a second row and debits the balance twice. |
| **Location** | `db/init/001_schema.sql` payments DDL ("no idempotency_key, no unique(charge_ref)"); `payment-service/app/payments.py` L74–79 and `servicing-service/app/payments.py` L74–77; call site `main.py` L68. |
| **Trigger** | Client timeout + retry, or double-click, on a card charge → two `payments` rows, balance debited twice, no key to collapse them. |
| **Risk** | **High.** Duplicate customer charges; compounded by D2 (no ledger to reconstruct). |
| **Attribution** | **Pre-existing** (baseline `60d1c37` servicing/payments). Not touched by our features. |
| **Measured** | **2026-08-02, Week 5, `scripts/repro_double_charge.py`.** Two sequential POSTs of one $100.00 intent: 2 rows, $200.00 captured. Eight concurrent POSTs of the same intent: 8 rows, $800.00 captured, all returning `200`. The client's "customers are just confused" reading does not survive the reproduction. |
| **Mitigation Path** | Client-minted `Idempotency-Key`, a **partial unique index** on `payments.idempotency_key`, and an insert-first write (`ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING`) that claims the key *before* the processor is contacted. The constraint rather than application code is the enforcement point, because two handlers write this table (see D23). Full design: `docs/spec-payments-week5.md` D1–D2; decided in **ADR 0013**. |
| **Status** | Open. **Fix specified (ADR 0013, Week 5); not built** — spec-only week. |

### D20: `audit_logs` is mutable + seeded with a plaintext PAN

| Field | Value |
|---|---|
| **ID** | D20 |
| **Finding** | `audit_logs` is an ordinary table — `UPDATE`/`DELETE` allowed, ships a `deleted_at` soft-delete column, no append-only trigger (contrast `decision_events`, which has one). App code can silently tombstone/alter audit rows. Separately, the seed writes a plaintext PAN into `audit_logs.detail`. |
| **Location** | `db/init/001_schema.sql` L124–132 (table + "ordinary, mutable table" comment); `db/init/002_seed.sql` L79 (`'charge req pan=4111111111111111 amount=250.00'`). |
| **Trigger** | Any `db.query("DELETE FROM audit_logs WHERE …")` / `UPDATE audit_logs …` succeeds unresisted (proven reachable by the same helper used for `UPDATE applications`). |
| **Risk** | **High.** README claims "SOX-controlled with full audit" — the audit trail is forgeable, and it already contains raw cardholder data (compounds D13). |
| **Attribution** | **Pre-existing** (baseline `e8bb2fa` schema + seed). We added the append-only `decision_events` alongside (ADR 0009) but deliberately did not convert `audit_logs`. |
| **Mitigation Path** | Add the `decision_events`-style `BEFORE UPDATE OR DELETE OR TRUNCATE` trigger to `audit_logs`; scrub the seeded PAN. Own PR. |
| **Status** | Open; documented; not fixed (out of scope). |

### D21: Postgres and Redis host-published in the base compose

| Field | Value |
|---|---|
| **ID** | D21 |
| **Finding** | Backend app ports (8001–8006) are correctly `expose`-only, but `postgres:5432` and `redis:6379` are published to the host in the base `docker-compose.yml`. Anyone on host/LAN with DB creds bypasses all app-layer authz and can read/write data or read session/resume keys directly. |
| **Location** | `docker-compose.yml` postgres `ports: 5432:5432` (L10–11), redis `ports: 6379:6379` (L23–24). |
| **Risk** | **Medium.** Direct-datastore exposure behind the gateway trust boundary. |
| **Attribution** | **Pre-existing** (baseline compose). Already noted as a deferred lower-priority item in the KB. |
| **Mitigation Path** | Drop the `ports:` on postgres/redis (keep `expose`); use a one-off admin container or SSH tunnel for local DB access. Own PR. |
| **Status** | Open; documented; not fixed (out of scope). |

### D22: Redactor missed unlabeled SSN with dot/slash/tab/multi-space separators — FIXED

| Field | Value |
|---|---|
| **ID** | D22 |
| **Finding** | The flat log redactor only caught unlabeled SSN in dash (`3a`) or single-space (`3a-bis`) form; unlabeled dotted `412.55.9981`, slashed `412/55/9981`, tabbed `412\t55\t9981`, and multi-space `412  55  9981` slipped into log lines. Labeled SSN with a two-char separator run (`"ssn":"412  55  9981"`) also slipped 3b (single optional separator). Distinct from D14 (encoded PII) — this is raw-separator grouping. |
| **Location** | `services/*/app/redactor.py` passes `3a-bis` and `3b` (canonical: `gateway/app/redactor.py`). |
| **Attribution** | **OURS** — introduced by the Week-1 PII redactor (`73ef737`). The one teeth finding in code our features touched. |
| **Fix** | Generalized `3a-bis` to a consistent non-dash separator (`([./])…\1`) OR whitespace run (`[ \t]{1,2}`), and widened `3b`'s separators from `?` to `{0,3}`. Edited the canonical gateway copy, resynced all 7 via `scripts/sync_redactor.sh` (redactor-drift stays green), added regression tests in `origination-service/tests/test_redactor.py` (unlabeled dot/slash/tab/double-space + labeled run + version/IPv4/phone false-positive guards). Kept the deliberate bare-9-digit non-redaction (documented tradeoff). |
| **Status** | **Fixed, on `main`** — merged as PR #9 (`170ed29`, 2026-08-02), 77 redactor tests passing, and held there by the blocking `redactor-drift` and `redaction-tests` jobs rather than by the branch that carried it. |

---

## Week 5 payments scoping — new entry

### D23: The charge handler exists twice, writing the same table

| Field | Value |
|---|---|
| **ID** | D23 |
| **Finding** | ADR 0004's decomposition copied the payment handler into `payment-service` and left the original routed in `servicing-service`. Both are live, both insert into the same `payments` table, and both carry the identical `# No idempotency check` comment. |
| **Location** | `services/payment-service/app/payments.py:76` and `services/servicing-service/app/payments.py:75` (the INSERTs); routes at `payment-service/app/routers/payments.py:19` and `servicing-service/app/main.py:60`. The two `_redacted_charge_req` helpers are kept byte-identical by hand, with a docstring asking future editors not to let them diverge. |
| **Risk** | **Medium.** Not a correctness risk on its own; it is a *fix-propagation* risk. Any control implemented in application code in one service silently does not apply to the other — which is the specific reason ADR 0013 puts idempotency enforcement in a database constraint rather than in a service. Divergence has precedent: `redactor.py` needed a blocking CI job (D15) for the same reason. |
| **Attribution** | **Ours by omission** — the ADR 0004 decomposition (`4c464b8`) copied rather than moved, and nothing since removed the original. |
| **Mitigation Path** | Retire the `servicing-service` charge path once `payment-service` is the sole writer, leaving servicing with `apply-payment` only. Until then the unique index from ADR 0013 binds both. Own PR. |
| **Status** | Open; documented Week 5; not fixed (out of scope for a spec week). Correctness risk covered by the constraint; maintenance cost remains. |

---

## Summary by Severity

| Severity | Finding | Status | Week 1 Action |
|---|---|---|---|
| **Critical** | D1: Hardcoded credentials | Open | Document, flag, schedule rotation (Week 2+). |
| **Critical** | D5: Plaintext PII in logs | Mitigated | ADR 0006's `PiiRedactor` merged in PR #2 (`1f89ac1`): a copy in all 7 services, held identical by the blocking `redactor-drift` job and covered by the blocking `redaction-tests` job; `intake.py` logs an allowlist rather than the payload. Residuals open: encoded PII (D14, deferred), no log rotation/30-day retention, no redaction at ingest for centralized logging, pre-redaction logs and backups unaudited. |
| **Critical** | D13: PAN/CVV in DB | Open | Document, flag, schedule tokenization (Week 2–3). |
| **High** | D2: Float money math | Partially mitigated | Week 4 (ADR 0012): the disclosure compute path is `Decimal` end to end and the authoritative `disclosures` record is integer minor units, held by the blocking `tila-vectors-gate`. Intake, decisioning, servicing, payments, and `balances` remain float. |
| **Medium** | D14: Encoded PII bypasses log redaction | Deferred | The log redactor matches literal shapes only, so percent-encoded (email=maria%40example.com, ssn=412%2D55%2D9981) and unicode-escaped (@) PII in uvicorn access-log query strings is not masked. Payload vector closed by allowlist logging; no sensitive route accepts PII via query/path today, so exposure is a client-crafted query param. Follow-up: bounded URL-decode + \uXXXX-unescape normalization pass in the (CI-synced) redactor, with regression tests for encoded email/SSN/phone. Not done now to avoid a byte-altering change to the shared redactor for a low-exposure case. |
| **Low** | D15: `redactor.py` duplicated per service (no shared package) | Mitigated | `services/*/app/redactor.py` is a near-identical copy in each of the 7 services — no shared module. Drift risk (a fix in one not reaching the others) is held closed by the **blocking** `redactor-drift` CI job, which fails the build if any copy diverges from the canonical; copies are resynced with `scripts/sync_redactor.sh`, never hand-edited. So this is a maintainability/structure cost, not an open leak path. Follow-up: extract a shared internal package (e.g. `libs/redaction`) so the copies collapse to one import; deferred because the CI gate already prevents divergence and a shared package adds packaging/build wiring across 7 services (YAGNI until a 2nd shared util appears). |
| **Low** | D17: Offer-replay schedule APR default 0 vs 7.99 | Open (residual) | Null-APR offer row would disclose 0% APR beside a 7.99%-computed schedule (`_offer_response_from_persisted`). Not reachable — `accept_offer` rejects null-APR, `build_offer` always writes one. Display-only; unify the default when next touched. |
| **Low** | D18: Fresh-insert vs replay offer schedule cent drift | Open (residual) | Retry returns a schedule reconstructed from the stored disclosure box (principal backed out via `amount_financed/0.97`, term via `round(total/monthly)`); float round-trip can drift a cent per row vs the fresh-insert response. Disclosure totals unaffected. Subsumed by D2. |
| **Low** | D16: RAG eval index is in-memory only (pgvector deferred) | Deferred (by design) | The `rag_eval` harness rebuilds an in-memory exact-cosine index each run and keeps no persistent vector store (ADR 0007 rule 6). Correct at the current 9-chunk corpus — brute-force cosine is microseconds and a vector DB would add latency for zero benefit. **Phase 2 trigger — build a `PgVectorIndex` behind the existing `Index` contract (`add`/`search`/`__len__`) when ANY holds:** (1) corpus grows past ~hundreds of chunks (brute-force starts to hurt); (2) vectors must persist/share across runs or processes; (3) more than one service queries the same vectors. Scoped in `docs/PHASE1-BEDROCK-PGVECTOR.md` § Phase 2. When triggered it is **its own PR**: `pgvector/pgvector:pg16` image, `CREATE EXTENSION vector` + schema migration, DB wiring into the (currently DB-free) harness, and an **ADR 0007 rule 6 amendment** with a PII re-review of the persistent store. Not started; the `Index` seam is the readiness, so no code carries the cost until then. The Bedrock embedding backend (Phase 1) is already built + smoke-tested and is independent of this. |
| **Critical** | D8: Servicing enforces no authz (IDOR + no maker-checker) | **Partially mitigated** | Teeth 2026-07-19 found any authenticated user could read/mutate any loan (serial-id IDOR) and move money on any account, servicing having no authz module. PR #32 (`f901894`, 2026-08-13) added `app/authz.py` and a guard on every route: internal-caller on `apply-payment`/`late-fee`/`peek`, money-role on `adjust-balance`/`waive-fee`, owner-or-role on `POST /payments` and every loan read. The IDOR is closed. **Open: maker-checker** — one money role still makes and approves its own balance adjustment; ADR 0017 (#40) proposes a second approver and is Proposed, not built. **Open: the gateway** still enforces no role authz on money actions, so servicing is the sole enforcement point. |
| **High** | D19: Payment double-charge (no idempotency key) | Open (pre-existing) | Teeth 2026-07-19. Retried `POST /payments` inserts a 2nd row + debits balance twice; no idempotency key / unique charge ref. Add key + unique index + replay-on-conflict. Own PR. |
| **High** | D20: `audit_logs` mutable + seeded plaintext PAN | Open (pre-existing) | Teeth 2026-07-19. No append-only trigger (has `deleted_at`), rows UPDATE/DELETE-able; seed writes raw PAN into `detail`. Forgeable "audit" contradicts README SOX claim. Add append-only trigger + scrub seed. Own PR. |
| **Medium** | D21: Postgres/Redis host-published in base compose | Open (pre-existing) | Teeth 2026-07-19. `5432`/`6379` published to host bypass app-layer authz. Drop `ports:` (keep `expose`). Own PR. |
| **Medium** | D22: Redactor missed unlabeled dot/slash/tab/multi-space SSN | **Fixed** | Teeth 2026-07-19. **The one finding in code our features introduced** (Week-1 redactor). Generalized `3a-bis` + widened `3b`; resynced 7 copies; regression tests added. On `main` — PR #9 (`170ed29`), held by the blocking `redactor-drift` and `redaction-tests` jobs. |
| **High** | D3: Unlocked read-modify-write on `balances` loses concurrent applies | Open — **fix specified, not built** | Week 5. Cited in code four times since the baseline, never logged and never measured. **Measured 2026-08-02:** 8 concurrent applies captured $800.00 and credited $600.00 — $200 taken, never applied. Fix is an atomic `UPDATE balances SET balance = balance - :amount` in one transaction with an append-only `payment_applications` row (ADR 0013, spec D3d). **Not fixed by the idempotency key** — distinct defect. |
| **Medium** | D23: The charge handler exists twice, writing the same `payments` table | Open (ours by omission) | Week 5. ADR 0004 copied the handler into `payment-service` and left the original routed in `servicing-service`; both INSERT into `payments`, both carry the same "no idempotency check" comment. Fix-propagation risk, same shape as D15. It is the reason ADR 0013 enforces idempotency with a DB constraint rather than in service code. Retire the servicing charge path; own PR. |
| **High** | D24: Self-decision block missed an officer's own account-linked-elsewhere submission | **Partially fixed** | Week 8. PR #38 review. `deny_self_decision` compared only `users.applicant_id` to `applications.applicant_id`; intake never linked the two, so an officer who self-submitted via the ordinary apply flow was not caught. Fixed for logged-in self-submits landing AFTER migration 0017 (`applications.submitted_by_user_id` captured at intake + a second comparison in the guard). Open: pre-migration rows and any anonymous self-submit are permanently NULL and indistinguishable from a genuine anonymous applicant at the SQL level — no fail-closed gate is possible without blocking the platform's primary anonymous-apply channel forever, not just a legacy backlog. Mitigated operationally via a manual audit query (`docs/runbook.md`). |
| **Low** | D25: A per-checkout `.env` can hold a password the shared data volume never had | Open (pre-existing) | Week 7 live verification, 2026-08-15. Postgres reads `POSTGRES_PASSWORD` only when initializing an empty data directory; the data volume is shared across checkouts while `.env` is per-checkout and git-ignored, so the two can disagree with nothing comparing them. Services report unhealthy and every DB-backed route 503s on a correctly built stack. The shared volume is **not** the defect — the pinned Compose project name is deliberate and correct, since published host ports allow only one stack per machine anyway. What is missing is detection and documentation: a runbook line naming this cause, and optionally a fail-fast check in `make up`. Cannot lose data; the connection is refused, not mis-written. |

---

## Week 8 governance scoping — new entry

### D24: Self-decision block only caught an officer whose account IS linked to the applicant — PARTIALLY FIXED

| Field | Value |
|---|---|
| **ID** | D24 |
| **Finding** | `authz.deny_self_decision` (client ask, 2026-08-12 governance §5) compared `users.applicant_id` to `applications.applicant_id` to refuse an officer deciding their own application. `intake.create_application` always INSERTs a fresh `applicants` row and never links it back to `users.applicant_id` (no such write exists outside `db/init/002_seed.sql`), and staff seed rows carry `users.applicant_id = NULL`. So an underwriter/admin who submitted an application through the ordinary (anonymous-by-design, ADR 0010) intake route, then decisioned it under their own officer role, was not blocked — the guard had no caller-applicant-id to compare. |
| **Location** | `services/origination-service/app/authz.py::deny_self_decision`; root cause was `services/origination-service/app/intake.py::create_application` (never wrote a submitter id); `db/init/001_schema.sql` `applications` table. |
| **Trigger** | An officer account applies through the borrower-facing apply flow using their own information, then opens the same application in the officer queue and runs a decision on it. |
| **Risk** | **High** (per PR #38 review). Same segregation-of-duties gap the control was built to close, reachable via the one path (self-service submission) the account-comparison design alone could not see. |
| **Attribution** | The control (`deny_self_decision`) is ours (`cd8c3e8`, this branch); the root cause (anonymous, unlinked intake) was **pre-existing ADR 0010 architecture**. |
| **Fix** | `applications.submitted_by_user_id` (migration `db/migrations/0017_applications_submitted_by_user_id.sql`, mirrored in `db/init/001_schema.sql`, with a readiness rung in `origination-service/app/config.py`). `POST /applications` now reads `X-User-Id` — the gateway forwards it for any session-bearing request, this route included — and `intake.create_application` persists it. `deny_self_decision` runs TWO independent checks: the original account-linkage comparison, plus `submitted_by_user_id == caller user id`, and blocks on either. Closes the reported scenario (officer self-submits while logged in AFTER this migration is live, then self-decides) without matching on identity fields. |
| **Residual (PR #38 review, round 3)** | `submitted_by_user_id` is NULL for every row that predates migration 0017 (the `ALTER TABLE ADD COLUMN` writes no backfill — there is nothing to backfill FROM, since no prior code ever captured the submitter), and NULL is also the permanent, correct value for every genuinely anonymous application going forward. **These two cases are identical at the SQL level, forever** — not a closing migration-window artifact, because ADR 0010's anonymous-apply channel is the intended common case and stays NULL by design after rollout too. So a fail-closed gate on `submitted_by_user_id IS NULL` was evaluated and rejected: it would permanently block the platform's primary intended intake channel, not just a legacy backlog. There is also no `schema_migrations`-style tracking table in this schema (migrations are hand-applied — CLAUDE.md, `migration-numbering-gate`), so no reliable per-deployment cutoff timestamp exists to scope a gate to "before this migration" even in principle; a hardcoded cutoff would be unverifiable per-environment guesswork, the same "false safety" migration 0008 explicitly rejected for continuation-token backfill. A staff account whose OWN `users.applicant_id` link exists is still caught (check 1, unaffected). What remains open is narrower than the original D24 gap: an officer who submitted anonymously (not merely unlinked) before this fix landed. Mitigated operationally, not in code — see `docs/runbook.md` "Known operational pain" for the manual back-book audit query — same class as migration 0008's own officer-mediated recovery path for its un-backfillable legacy rows. |
| **Status** | **Partially fixed, on `main`.** Merged as PR #38 (`1987dcd`, 2026-08-14); `b775eee` tracked the original gap and `89bd801` closed the reported logged-in self-submit scenario, and both are ancestors of `main`. Pre-migration/genuinely-anonymous-submit rows remain open by construction; pinned (not silently) by `test_legacy_null_submitter_self_decision_is_not_blocked_by_code` (`tests/test_authz.py`) and route-level equivalents in `tests/test_decision_route.py` / `tests/test_assistant.py`. Other regression coverage: `test_self_submitted_application_not_linked_is_now_blocked` / `test_self_submitted_by_someone_else_is_allowed` / `test_anonymously_submitted_application_is_allowed` (`tests/test_authz.py`), readiness rung tests in `tests/test_db_readiness.py`. |

---

## Week 7 live verification — new entry

### D25: A per-checkout `.env` can hold a password the shared data volume has never had

| Field | Value |
|---|---|
| **ID** | D25 |
| **Finding** | Postgres reads `POSTGRES_PASSWORD` only when it initializes an empty data directory. The data volume is shared across every checkout on the machine, while `.env` is per-checkout and git-ignored. So a second checkout's `POSTGRES_PASSWORD` re-initializes nothing — it simply authenticates against a volume that has never held that value. Nothing compares the two, and nothing in the repository documents that they must agree. |
| **Not the shared volume itself** | The shared volume is deliberate and is not the defect. `docker-compose.yml:1` pins the Compose project name, so every `make up` targets one stack from any directory. That is the correct behaviour here: the base compose publishes host ports 5432, 6379, 8000 and 3000, so only one stack can run on a machine regardless, and without the pin each checkout would start a separate project that fails to bind those ports and leaves an orphan data volume behind. The pin dates to the initial scaffold (`e8bb2fa`), not to a later patch. |
| **Location** | `docker-compose.yml:1` (`name: meridian-lending`) and `docker-compose.yml:226-227` (`volumes: pgdata:`), with `.env` ignored at `.gitignore:29`. |
| **Symptom** | Services report unhealthy and every DB-backed route 503s on a stack that is otherwise correctly built. Observed 2026-08-15 during the live verification of the payment-span correlation fix: servicing-service and payment-service both failed DB auth on a fresh `make up` until the `meridian` role's password was reset over the local trust socket. No data was touched; the reset moved the volume from a broken state to a working one. |
| **Risk** | **Low** for correctness, **medium** for time. It cannot corrupt or lose data — Postgres refuses the connection rather than writing anything. The cost is an unbounded debugging session, because the failure presents as a service or configuration problem while every layer above the volume is correct, so the search starts in the wrong place. |
| **Attribution** | **Pre-existing**, and the design half is intentional. What is missing is not isolation but detection and documentation: `pgdata`, the shared volume, and the Compose project name are mentioned nowhere in `README.md`, `CLAUDE.md` or `docs/`. |
| **Mitigation Path** | Two options, and they compose. (a) Document it: `docs/runbook.md` states that a DB-auth 503 on a fresh `make up` means the `.env` password disagrees with the already-initialized volume, with the reset command. This is the smaller and probably sufficient remedy — the gap is that nobody wrote it down. (b) Have `make up` fail fast with a named error when `.env`'s `POSTGRES_PASSWORD` does not authenticate, rather than surfacing as a health-check failure. Dropping the pinned project name to give each worktree its own volume was considered and is **not** proposed: it fights the single-stack design the published ports already enforce, and costs a re-seed and a duplicate volume per checkout. |
| **Status** | Open; documented; not fixed. Neither a client-visible defect nor a blocker for the agentic and RAG capability landing on `main`, which is what sends it to this register rather than into a cycle. |

---

**Week 5 status changes (2026-08-02, spec-only — designs recorded, nothing built):**
**D19** measured and design recorded (client-minted key + partial unique index + insert-first
claim before the processor call). **D13** design recorded, superseding ADR 0003 — CVV dropped
and purged, PAN tokenized in the browser, purge migration must rewrite the table or the bytes
survive. **D8** split into (a) a missing `X-Internal-Service` gate on `apply-payment`, which
is money creation and is not an authorization model problem, and (b) genuine RBAC, which
stays deferred. **D2** narrowed: the atomicity half is now D3 with a fix specified; the float
columns and the ledger stay open.

---

## Week 1 Actions

✓ **D1:** Documented; flagged for rotation (Week 2).  
✓ **D2:** Documented; flagged for Decimal migration (Week 2+).  
✓ **D5:** **Done for Week 1 (ADR 0006).** `PiiRedactor` code + tests merged in PR #2 (`1f89ac1`), one copy per service, guarded by the blocking `redactor-drift` and `redaction-tests` jobs. Existing logs flagged (not deleted, out of scope). The Week-2/Week-3 rows of the mitigation path — rotation/retention, redaction at ingest, backup audit — are still open, so D5 reads Mitigated, not Fixed.  
✓ **D13:** Documented; flagged for tokenization (Week 2–3).  

---

## Next Steps

- **Week 1:** Complete logging redaction (D5) and verify via integration tests.
- **Week 2:** Rotate credentials (D1), begin tokenization design (D13).
- **Week 2–3:** Migrate to Decimal for money math (D2). *Week 4: done for the disclosure compute path (ADR 0012); intake, decisioning, servicing, payments still to do.*
- **Ongoing:** Review new debt as it emerges; update this log.
