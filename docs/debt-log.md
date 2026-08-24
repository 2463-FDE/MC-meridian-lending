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
| **Mitigation Path** | Make the mutation a single atomic statement — `UPDATE balances SET balance = balance - :amount WHERE loan_id = :id` — committed in the same transaction as an append-only `payment_applications` row unique on `payment_id`. Specified as **D3d** in `docs/spec-payments-week5.md`; decided in **ADR 0020** (split out of ADR 0013, which named the defect first but does not decide it). Distinct from the idempotency key: the key collapses duplicate *intents*, but two genuinely distinct concurrent payments still race. |
| **Status** | **Mitigated.** ADR 0020 decides the fix and is on `main` (PR #78). Built as migration `0019_payment_applications.sql` plus a single-statement apply in `services/servicing-service/app/balance.py`: an `INSERT ... SELECT` into `payment_applications` (`UNIQUE (payment_id)`) and an `UPDATE` that computes from the stored value inside the statement, so concurrent applies serialize on the row lock instead of both reading one opening figure. The loan and the amount come off the `payments` row — `amount` is removed from `ApplyPaymentIn` and from the body `payment-service` sends, closing a path where an authorized internal caller could credit a figure that was never captured. Merged as PR #77 (`d649bc6`, merge `ceda4e2`, characterization follow-up `bbe31d7`, 2026-08-24), proven by `make prove` — `test_lost_update.py` flipped from failing to passing — and held by the blocking `atomic-apply-gate` in `.github/workflows/ci.yml`, which runs `test_lost_update.py`, `test_atomic_apply.py` and `test_payment_applications_ddl.py` outside the `|| true` matrix. **One thing the Week-5 spec did not anticipate:** spec D3(d)'s predicate (`status IN ('captured','settled')`) could never fire against the shipped D19 ordering, which applies while the row is still `processing`; both handlers now finalize to `captured` first and downgrade to `captured_unapplied` when the apply refuses, and `_RETIRE_SQL` in both services requires a `payment_applications` row before retiring a `captured` key, so a crash between finalize and apply cannot free the key. Not **Fixed** — the residual is named: `adjust_balance` and `waive_fee` keep the unlocked read-modify-write shape on `balance` and `past_due`, a deliberate scope decision pinned by `test_characterization_balance.py` and carded as **D32** below. Will not fix the float column (D2) or the waterfall (D14). |

**Bookkeeping note:** the D-numbers used in service code and the ones defined in this log
have drifted. Code cites `D3`, `D4`, `D7`, `D11`, `D12`, and `D14`; this log defines `D14`
as *"Encoded PII bypasses log redaction"* while `servicing-service/app/main.py:83` uses
`D14` for *"no payment waterfall"*. D3 is reconciled here, and **D4 is reconciled in the
2026-08-22 sweep below** — both now have entries matching what the code already cites.
`D7`, `D11`, `D12` and the `D14` collision are left alone — renumbering live code comments
is a separate mechanical change.

---

### D5: Plaintext PAN/CVV/SSN in Logs

| Field | Value |
|---|---|
| **ID** | D5 |
| **Finding** | *(As found 2026-07-01, and no longer true of `main` — see Status.)* Payment and origination services log full request/response bodies, including plaintext PAN, CVV, and SSN. |
| **Location** | `services/payment-service/app/logging_config.py` (lines 1–4): docstring — "writes the full charge request body (PAN, CVV, SSN) at INFO. No redaction." |
| | `services/payment-service/app/payments.py` (lines 23–27): `charge()` logs the full request body `{"pan","cvv","ssn","amount","loan_id","name"}` at INFO on `POST /payments`. |
| | `services/origination-service/app/logging_config.py` (line 3–4): "Logs the full request body on every POST — including PII. No redaction." |
| | `services/origination-service/app/intake.py` (line 15): `log.info("POST /applications intake req=%s", payload)` — payload includes SSN, email, phone. |
| | **Log files:** `logs/payment-service.log`, `logs/origination-service.log` contain unredacted cardholder data. |
| | **Sample from repo handover:** `INFO charge req={"pan":"4111111111111111","cvv":"123","ssn":"412-55-9981","amount":250.00}` |
| | *(The two `logging_config.py` docstrings quoted above are the originals and no longer exist. PR #2 (`1f89ac1`) rewrote both on `main`: origination's now opens "Logging setup with PII redaction", payment's "Now redacts PII before writing to logs/payment-service.log". The log files named above are the pre-redaction ones, which still hold plaintext and are still unaudited — see Status.)* |
| **Risk** | **Critical.** PCI-DSS 3.4: "Rendering PAN unreadable anywhere it is stored (including on portable digital media, backup media, and **in logs**)." |
| | If log files are: |
| | - Backed up to S3/tape (unencrypted or with lost key), PII is exposed. |
| | - Aggregated to a central logging service (Loki, ELK, Splunk) without redaction, PII is searchable. |
| | - Left on disk after server failure, physical recovery exposes PII. |
| | Violation triggers: fines (up to $100k+ per incident under state laws), customer breach notifications, reputational damage. |
| **Current Impact** | *(First sentence as found 2026-07-01, no longer true of `main`.)* Logs were actively created and written to disk daily with PII unredacted; new log lines are redacted as of PR #2 (`1f89ac1`). Still true today: no retention or rotation policy exists (`logging_config.py` configures no rotating handler), so logs grow without bound, and the pre-redaction files already on disk are neither audited nor deleted. If a server is decommissioned, those files may be left in place. |
| **Mitigation Path** | **Week 1 (NOW):** Implement `PiiRedactor` class; apply to all 7 services' logging (ADR 0006). Redact PAN, CVV, full SSN, email, phone before writing to disk. Preserve last 4 of SSN for audit trails. |
| | **Week 1 (ongoing):** Flag existing log files (in this debt-log). Do not delete; archive separately (out of scope). |
| | **Week 2:** Implement log rotation + deletion (30-day retention). |
| | **Week 2:** Implement centralized logging (Loki/ELK) with redaction at ingest. |
| | **Week 3:** Audit all existing backups; re-encrypt or delete any containing plaintext PII. |
| **Status** | **Mitigated** — the Week-1 redaction row is done and merged; the Week-2/Week-3 rows are not. `PiiRedactor` (ADR 0006) merged in PR #2 (`1f89ac1`) and now ships as `services/*/app/redactor.py` in all 7 services, kept identical by the blocking `redactor-drift` job and covered by six suites (`test_logging_redaction.py` in kyc/origination/payment/servicing, plus origination's `test_redactor.py` and `test_pii_matrix.py`) under the blocking `redaction-tests` job. `intake.py` also logs an allowlist of fields rather than the payload, so the redactor is a backstop there, not the only control. **Not closed.** Open residuals: encoded PII still bypasses the redactor (D22 closed the raw-separator gaps; D14 covers percent- and unicode-encoded PII and stays deferred); no log rotation or 30-day retention exists (`logging_config.py` configures no rotating handler); no centralized logging with redaction at ingest; the pre-redaction log files and any backups containing them are flagged here but not audited. |

---

### D13: PAN and CVV Stored in Database — CVV DELETED (D13a), PAN OPEN (D13b)

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
| **Status** | **Split, and only half is closed. D13a (CVV) — Mitigated.** Built 2026-08-24 as migration `0020_payments_drop_cvv.sql` plus the removal of the column from the init DDL, the seed, both charge handlers (`services/payment-service/app/payments.py`, `services/servicing-service/app/payments.py`), both request models and the frontend's hardcoded literal. The migration NULLs the values, drops the column, asserts the drop, then `VACUUM FULL`s the table — `DROP COLUMN` only marks the attribute dropped and the preceding `UPDATE` leaves the old row versions as dead tuples, so without the rewrite every CVV stays readable in the data files. Held by the blocking `no-sad-gate` and by a readiness rung in both services (`_no_stored_sad_ready`) that reports `schema_not_ready:payments.cvv_present`, so a volume that skipped the migration is unhealthy and 503s a charge rather than serving over a schema holding prohibited data. Operator procedure in `docs/runbook.md`. Not **Fixed**: the "Not covered" row above still stands — WAL segments, replicas and any backup taken before the rewrite still contain the values, which is a retention action nobody has taken. **D13b (PAN) — Open, and it still blocks production deployment.** The `pan` column is untouched and ADR 0013 Decision 2's tokenization is not built: it needs a provider decision (Stripe / AWS Payment Cryptography / self-hosted HSM) and a card entry surface, and this platform has neither — the only card input is a hardcoded test PAN in the servicing screen, and the borrower-facing form is ADR 0013 Decision 3, also unbuilt. Two other entries wait on it: D19's fingerprint hashes the PAN because no token column exists, and vector R3c stays red until `bank_token` does (D4b). |

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
| **Status** | **Partially mitigated.** Both halves this entry named are closed on `main`; the maker-checker half is not. PR #32 (`f901894`, 2026-08-13) added `services/servicing-service/app/authz.py` — the module this entry says does not exist — and every route now guards before it reads or writes: `require_internal_caller` on `apply-payment` (`app/main.py:171`), `late-fee` (`:259`) and `/reconciliation/peek` (`:285`), which is (a); `require_money_role` on `adjust-balance` (`:228`) and `waive-fee` (`:245`), `require_money_role_or_owner` on `POST /payments` (`:87`), and `require_staff_or_owner` on the balance read (`:200-208`) and on the loan detail, schedule and payments reads (`app/routers/loans.py:119`, `:144`, `:164`), with `require_staff` on the list route (`:37`) — which is the RBAC half of (b). Ownership did not need ADR 0010's identity flow after all: the guard takes the owner from `X-User-Id` rather than from a signup flow, which is the ADR 0010 seed decision. **Open: the maker-checker half.** A single money role still both makes and approves a balance adjustment; no second approver exists. ADR 0017 (#40, `b6f1149`) proposes it and is Proposed, not built. **Also open:** the gateway still does not enforce role authz on money actions — it authenticates and forwards `X-User-Role`, so servicing is the only enforcement point (the `services/gateway/app/main.py` header comment says so, though its tail — that servicing "also doesn't check" and "any authenticated user can adjust balances" — is itself now false and is a code comment this log does not own). Not closed until maker-checker lands. |

### D19: Payment charge has no idempotency key (double-charge on retry)

| Field | Value |
|---|---|
| **ID** | D19 |
| **Finding** | `charge()` used to insert a row then call `apply_payment` with no dedupe, so a retried/double-submitted `POST /payments` inserted a second row and debited the balance twice. Fixed at capture: `payments.idempotency_key` sits under a partial unique index (PR #63) and both charge handlers now claim it insert-first via `claim_or_branch()` before the processor is contacted (PR #65), so a retry under the same key returns the original outcome instead of a second row. |
| **Location** | Claim path: `payment-service/app/payments.py` `_CLAIM_SQL` (L175–183) and `claim_or_branch()` (L293); mirrored in `servicing-service/app/payments.py`. The DDL half is in place: `db/init/001_schema.sql` L142–143 and L220–221, mirrored by `db/migrations/0018_payments_idempotency.sql`. |
| **Trigger** | Historical/measured failure (pre-fix): client timeout + retry, or double-click, on a card charge → two `payments` rows, balance debited twice, no key to collapse them. Residual trigger today: a processor-side duplicate or any break older than PR #65 — an exact retry under one key no longer collapses into two rows. |
| **Risk** | **High.** Duplicate customer charges; compounded by D2 (no ledger to reconstruct). |
| **Attribution** | **Pre-existing** (baseline `60d1c37` servicing/payments). Not touched by our features. |
| **Measured** | **2026-08-02, Week 5, `scripts/repro_double_charge.py`.** Two sequential POSTs of one $100.00 intent: 2 rows, $200.00 captured. Eight concurrent POSTs of the same intent: 8 rows, $800.00 captured, all returning `200`. The client's "customers are just confused" reading does not survive the reproduction. |
| **Mitigation Path** | Client-minted `Idempotency-Key`, a **partial unique index** on `payments.idempotency_key`, and an insert-first write (`ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING`) that claims the key *before* the processor is contacted. The constraint rather than application code is the enforcement point, because two handlers write this table (see D23). Full design: `docs/spec-payments-week5.md` D1–D2; decided in **ADR 0013**. |
| **Status** | **Mitigated.** Built as migration `0018_payments_idempotency.sql` plus an insert-first claim in both charge handlers; held by the blocking `payment-idempotency-gate`. Not **Fixed**: three rows of the Mitigation Path above remain open — the stuck-row reaper is carded as **D30**, the specified `card_token`/`bank_token` fingerprint waits on tokenization (**D13**), and vector R3c stays red until `bank_token` exists (**D4b**). |
| **Residuals** | Three, each deliberate. (1) The fingerprint hashes the PAN rather than a token, because neither token column exists until D13b (D13a deleted the CVV column; it did not add the token columns) — so a card-derived value sits beside the PAN that D13 deletes, and it goes when the token replaces it. (2) **R3c is unsatisfiable and stays red**: this codebase has no bank instrument field, so two ACH submissions reusing one key against different accounts hash equal and the second is answered as a replay. (3) The row cannot distinguish captured-and-applied from captured-unapplied, so replaying a request that first returned `424` returns a `200` replay; closing that needs the `payment_applications` record (spec D3(c)), which is **D3d**'s change. |

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
| **Critical** | D13: PAN/CVV in DB | **CVV deleted 2026-08-24 (D13a); PAN open (D13b)** | D13a: migration 0020 NULLs, drops and rewrites the table, both handlers stop writing it, held by the blocking `no-sad-gate` and a fail-closed readiness rung. D13b: tokenization still unbuilt and still blocks production — it needs a provider decision and a card entry surface that does not exist. Pre-rewrite backups and WAL still hold the purged values. |
| **High** | D2: Float money math | Partially mitigated | Week 4 (ADR 0012): the disclosure compute path is `Decimal` end to end and the authoritative `disclosures` record is integer minor units, held by the blocking `tila-vectors-gate`. Intake, decisioning, servicing, payments, and `balances` remain float. |
| **Medium** | D14: Encoded PII bypasses log redaction | Deferred | The log redactor matches literal shapes only, so percent-encoded (email=maria%40example.com, ssn=412%2D55%2D9981) and unicode-escaped (@) PII in uvicorn access-log query strings is not masked. Payload vector closed by allowlist logging; no sensitive route accepts PII via query/path today, so exposure is a client-crafted query param. Follow-up: bounded URL-decode + \uXXXX-unescape normalization pass in the (CI-synced) redactor, with regression tests for encoded email/SSN/phone. Not done now to avoid a byte-altering change to the shared redactor for a low-exposure case. |
| **Low** | D15: `redactor.py` duplicated per service (no shared package) | Mitigated | `services/*/app/redactor.py` is a near-identical copy in each of the 7 services — no shared module. Drift risk (a fix in one not reaching the others) is held closed by the **blocking** `redactor-drift` CI job, which fails the build if any copy diverges from the canonical; copies are resynced with `scripts/sync_redactor.sh`, never hand-edited. So this is a maintainability/structure cost, not an open leak path. Follow-up: extract a shared internal package (e.g. `libs/redaction`) so the copies collapse to one import; deferred because the CI gate already prevents divergence and a shared package adds packaging/build wiring across 7 services (YAGNI until a 2nd shared util appears). |
| **Low** | D17: Offer-replay schedule APR default 0 vs 7.99 | Open (residual) | Null-APR offer row would disclose 0% APR beside a 7.99%-computed schedule (`_offer_response_from_persisted`). Not reachable — `accept_offer` rejects null-APR, `build_offer` always writes one. Display-only; unify the default when next touched. |
| **Low** | D18: Fresh-insert vs replay offer schedule cent drift | Open (residual) | Retry returns a schedule reconstructed from the stored disclosure box (principal backed out via `amount_financed/0.97`, term via `round(total/monthly)`); float round-trip can drift a cent per row vs the fresh-insert response. Disclosure totals unaffected. Subsumed by D2. |
| **Low** | D16: RAG eval index is in-memory only (pgvector deferred) | Deferred (by design) | The `rag_eval` harness rebuilds an in-memory exact-cosine index each run and keeps no persistent vector store (ADR 0007 rule 6). Correct at the current 9-chunk corpus — brute-force cosine is microseconds and a vector DB would add latency for zero benefit. **Phase 2 trigger — build a `PgVectorIndex` behind the existing `Index` contract (`add`/`search`/`__len__`) when ANY holds:** (1) corpus grows past ~hundreds of chunks (brute-force starts to hurt); (2) vectors must persist/share across runs or processes; (3) more than one service queries the same vectors. Scoped in `docs/PHASE1-BEDROCK-PGVECTOR.md` § Phase 2. When triggered it is **its own PR**: `pgvector/pgvector:pg16` image, `CREATE EXTENSION vector` + schema migration, DB wiring into the (currently DB-free) harness, and an **ADR 0007 rule 6 amendment** with a PII re-review of the persistent store. Not started; the `Index` seam is the readiness, so no code carries the cost until then. The Bedrock embedding backend (Phase 1) is already built + smoke-tested and is independent of this. |
| **Critical** | D8: Servicing enforces no authz (IDOR + no maker-checker) | **Partially mitigated** | Teeth 2026-07-19 found any authenticated user could read/mutate any loan (serial-id IDOR) and move money on any account, servicing having no authz module. PR #32 (`f901894`, 2026-08-13) added `app/authz.py` and a guard on every route: internal-caller on `apply-payment`/`late-fee`/`peek`, money-role on `adjust-balance`/`waive-fee`, owner-or-role on `POST /payments` and every loan read. The IDOR is closed. **Open: maker-checker** — one money role still makes and approves its own balance adjustment; ADR 0017 (#40) proposes a second approver and is Proposed, not built. **Open: the gateway** still enforces no role authz on money actions, so servicing is the sole enforcement point. |
| **High** | D19: Payment double-charge (no idempotency key) | **Mitigated** | Teeth 2026-07-19; measured 2026-08-02 (8 concurrent POSTs of one $100 intent captured $800.00). Closed by migration 0018's partial unique index on `payments.idempotency_key` plus an insert-first claim in BOTH charge handlers, held by the blocking `payment-idempotency-gate`. Not Fixed: reaper carded as D30, token-based fingerprint waits on D13, R3c red until D4b. |
| **High** | D20: `audit_logs` mutable + seeded plaintext PAN | Open (pre-existing) | Teeth 2026-07-19. No append-only trigger (has `deleted_at`), rows UPDATE/DELETE-able; seed writes raw PAN into `detail`. Forgeable "audit" contradicts README SOX claim. Add append-only trigger + scrub seed. Own PR. |
| **Medium** | D21: Postgres/Redis host-published in base compose | Open (pre-existing) | Teeth 2026-07-19. `5432`/`6379` published to host bypass app-layer authz. Drop `ports:` (keep `expose`). Own PR. |
| **Medium** | D22: Redactor missed unlabeled dot/slash/tab/multi-space SSN | **Fixed** | Teeth 2026-07-19. **The one finding in code our features introduced** (Week-1 redactor). Generalized `3a-bis` + widened `3b`; resynced 7 copies; regression tests added. On `main` — PR #9 (`170ed29`), held by the blocking `redactor-drift` and `redaction-tests` jobs. |
| **High** | D3: Unlocked read-modify-write on `balances` loses concurrent applies | **Mitigated 2026-08-24 (#77)** | Week 5. Cited in code four times since the baseline, never logged and never measured. **Measured 2026-08-02:** 8 concurrent applies captured $800.00 and credited $600.00 — $200 taken, never applied. Fixed by ADR 0020 (atomic `UPDATE balances SET balance = balance - :amount` in one statement with an append-only `payment_applications` row, spec D3d) — on `main`, PR #77 (`d649bc6`, merge `ceda4e2`), migration 0019, held by the blocking `atomic-apply-gate`. **Not fixed by the idempotency key** — distinct defect. Residual: `adjust_balance`/`waive_fee` keep the old shape (**D32**). |
| **Medium** | D23: The charge handler exists twice, writing the same `payments` table | Open (ours by omission) | Week 5. ADR 0004 copied the handler into `payment-service` and left the original routed in `servicing-service`; both INSERT into `payments`, both carry the same "no idempotency check" comment. Fix-propagation risk, same shape as D15. It is the reason ADR 0013 enforces idempotency with a DB constraint rather than in service code. Retire the servicing charge path; own PR. |
| **High** | D24: Self-decision block missed an officer's own account-linked-elsewhere submission | **Partially fixed** | Week 8. PR #38 review. `deny_self_decision` compared only `users.applicant_id` to `applications.applicant_id`; intake never linked the two, so an officer who self-submitted via the ordinary apply flow was not caught. Fixed for logged-in self-submits landing AFTER migration 0017 (`applications.submitted_by_user_id` captured at intake + a second comparison in the guard). Open: pre-migration rows and any anonymous self-submit are permanently NULL and indistinguishable from a genuine anonymous applicant at the SQL level — no fail-closed gate is possible without blocking the platform's primary anonymous-apply channel forever, not just a legacy backlog. Mitigated operationally via a manual audit query (`docs/runbook.md`). |
| **Low** | D25: A per-checkout `.env` can hold a password the shared data volume never had | Open (pre-existing) | Week 7 live verification, 2026-08-15. Postgres reads `POSTGRES_PASSWORD` only when initializing an empty data directory; the data volume is shared across checkouts while `.env` is per-checkout and git-ignored, so the two can disagree with nothing comparing them. Services report unhealthy and every DB-backed route 503s on a correctly built stack. The shared volume is **not** the defect — the pinned Compose project name is deliberate and correct, since published host ports allow only one stack per machine anyway. What is missing is detection and documentation: a runbook line naming this cause, and optionally a fail-fast check in `make up`. Cannot lose data; the connection is refused, not mis-written. |
| **High** | D26: Three of four scored features are unverified applicant assertions, with no provenance on the record | Open (pre-existing) | Agentic scoping, 2026-08-22. Only `delinquency_history` comes from the bureau; `payment_burden`, `income_sufficiency` and `employment_tenure` are computed from applicant-typed income, debt and tenure that nothing verifies (`ge=0` bounds are the whole validation, and no verification step exists anywhere in the tree). Those three carry reason codes R02/R03/R04, so adverse-action notices name figures the platform never checked. Applicant-controlled features span ~182 score points against a 60-point gap between the approve and deny cutoffs. `decision_events.inputs` has no field distinguishing a verified figure from a stated one. Mitigation is three rungs: provenance field, a written stated-income policy, then a document store plus a consistency check feeding refer routing only. |
| **High** | D27: Passwords are unsalted SHA-256 | Open (pre-existing) | Sweep, 2026-08-22. `gateway/app/auth.py:32` is a bare single-round `sha256(password)` with no salt and no work factor. One precomputed table covers every user, and identical passwords produce identical hashes, so reuse across accounts is visible without cracking anything — including the `admin` and `underwriter` money roles. Any read of the shared `users` table is enough. Kept deliberately and documented in the module docstring, but never logged here, so nothing has scheduled it. Fix: argon2id/bcrypt with per-user salt and rehash-on-login so rows drain without a forced reset (needs a dependency — ask first). |
| **Medium** | D28: Equal nested timeouts, so the outer one can never fire first | Open (ours, partly) | Sweep, 2026-08-22. `origination-service/app/clients.py:21` sets `_TIMEOUT = 30.0` on every downstream call; `decision-service/app/decision.py:78` gives the bureau pull the same 30. The outer budget is not smaller than the inner, so origination cannot tell a slow bureau from a dead service, and no deadline is passed down. No retry anywhere in origination — which is right for the billable, inquiry-generating bureau pull and unexamined for everything else. Compounds ADR 0009 §6's deferred load behaviour. Found stale `CLAUDE.md` text alongside it ("no timeout/retry contract"), corrected. |
| **Medium** | D4: The outcome-only `decisions` table is still what the offer path reads | Open (pre-existing; code cited D4 since the baseline, no entry until now) | Sweep, 2026-08-22. `decisions` holds `app_id` + `outcome` and nothing else; ADR 0009's `decision_events` is the real record but did not retire it. `origination-service/app/routers/offers.py:80` gates offer creation on the thin table, and three officer reads join it. One atomic statement writes both today, but no constraint or append-only guard enforces agreement, so a manual UPDATE moves the offer gate while the append-only history still looks clean. Point readers at `decision_events`; guard or drop the table. |
| **Medium** | D29: The set of controls that cannot regress is a hand-maintained allowlist | Open (ours) | Sweep, 2026-08-22. `.github/workflows/ci.yml:30,33` runs the whole matrix under `continue-on-error` + `\|\| true`, so protection means hand-copying a control into its own blocking job and the default for a new one is unprotected. This is the mechanism the 4.5pp APR defect came through, and `CLAUDE.md` names the live instance: `adjust_balance` and `waive_fee` (D32) keep the unlocked read-modify-write shape — `apply_payment` and the red D3 lost-update test moved into the blocking `atomic-apply-gate` on 2026-08-24 (PR #77). Not an argument for flipping the flag — the tolerated failures are deliberate D2 documentation. Fix: enumerate the known-red tests and let everything outside that list block. |
| **Medium** | D30: A crash between claiming an idempotency key and finishing the capture strands the row, and its key is never released | Open (ours, introduced by the D19 fix) | 2026-08-22. The D19 claim writes `status='processing'` before the capture completes. A crash in between leaves that row non-terminal forever, and **only a terminal intent releases its key** — so every later retry under that key answers `409` rather than replaying or charging, and the customer cannot pay. Smaller than the double charge it replaces, but genuinely new. Fix: the reaper `docs/spec-payments-week5.md` D2 specifies — resolve rows older than `PAYMENT_PROCESSING_TIMEOUT_MINUTES` by querying the processor for that row's `processor_idempotency_key` (stamped at insert precisely so it survives this crash), then retire expired keys in the same pass, stuck rows first. Deliberately out of scope for the D19 PR to keep it reviewable; carded rather than shipped silently. |

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


### D30: A crash between claiming a key and finishing the capture strands the row and its key

| Field | Value |
|---|---|
| **ID** | D30 |
| **Finding** | The D19 claim is insert-first: the row is written with `status='processing'` and its `idempotency_key` held, *then* the capture runs, *then* the row is moved to a terminal status. A crash, a killed container, or an unhandled exception between those steps leaves the row `processing` permanently. Because **only a terminal intent releases its key** (`_RETIRE_SQL` and the spec's step 2a both require `status IN ('captured','failed','settled','returned')`), that key is never retired — not by the expiry window, and not by a later request. |
| **Location** | `services/payment-service/app/payments.py` and `services/servicing-service/app/payments.py`, the `claim_or_branch` / `_FINALIZE_SQL` pair. |
| **Trigger** | Any abnormal termination during a capture. |
| **Risk** | **Medium.** The customer cannot pay under that key: every retry answers `409 in_flight` forever, and the window that would normally free the key deliberately does not apply to an unfinished intent. No money is lost or double-taken — this fails closed, which is why it is Medium and not High — but a stuck row needs an operator to resolve today. |
| **Attribution** | **Ours, and new.** This failure did not exist before the D19 fix, because there was no key to strand. It is the cost of claiming the key before contacting the processor, which is the property that makes the double charge impossible; the alternative (claim after) reopens the defect. |
| **Not a reason to shorten the window** | Retiring the key of an unfinished intent would free it for a *new* charge while the original is still live, reintroducing D19 at the boundary, and would destroy the value the reaper resolves the row by. The terminal-only condition is correct; what is missing is the job that makes rows terminal. |
| **Mitigation Path** | The stuck-row reaper already specified in `docs/spec-payments-week5.md` D2: resolve rows older than `PAYMENT_PROCESSING_TIMEOUT_MINUTES` by querying the processor under that row's `processor_idempotency_key` — stamped at insert precisely so it survives a crash before `processor_ref` is written — then retire expired keys in the same pass, **stuck rows first**, so a row that reaches a terminal status in a pass has its key released in that same pass rather than a cycle later. Vectors R8 and R9 in the same spec cover it. |
| **Status** | Open; carded 2026-08-22 as a deliberate scope decision on the D19 PR, which was already twice its line ceiling. Not started. |

---

## Agentic scoping 2026-08-22 — new entry

### D26: Three of the four scored features are unverified applicant assertions, and the decision record cannot say which

| Field | Value |
|---|---|
| **ID** | D26 |
| **Finding** | The scorecard has four features (`model_vendor._feature_contributions`). Exactly one — `delinquency_history` — derives from an external source (the bureau score). The other three are computed entirely from figures the applicant types in and nothing checks: `payment_burden` and `income_sufficiency` from `annual_income`, `monthly_debt`, `requested_amount`, `term_months`; `employment_tenure` from `employment_years`. Validation is bounds-only (`ge=0`), with no upper bound, no cross-field consistency check, and no verification step anywhere in the tree — a repo-wide grep for income/employment verification, paystub, W-2, VOE, bank statement or tax return returns one hit, and it is a string inside an LLM test fixture. `kyc_checks` verifies identity only (name, DOB, address, SSN); it has never touched income or employment. Three of the four adverse-action reason codes — R02 excessive obligations, R03 income insufficient, R04 length of employment — map one-to-one onto the three unverified features, so the platform issues Reg B principal reasons naming figures it never verified. |
| **Second half of the finding** | There is also no provenance flag. `decision.py::_run_decision` persists `inputs` into `decision_events` as the evidentiary record, but no field on that record distinguishes a verified figure from a stated one, and no such column exists to set. So the record cannot answer "was this income verified?" even for an application where an officer verified it out of band. That half is cheap to close and is independent of building any verification capability. |
| **Location** | `services/decision-service/app/model_vendor.py:26-52` (the four features); `services/decision-service/app/decision.py:338-345` (scoring inputs) and `:352` (the persisted record); `services/decision-service/app/reasons.py:12` (R02/R03/R04); `services/decision-service/app/schemas.py:15-17` and `services/origination-service/app/schemas.py:57,61` (the `ge=0` bounds, which is the whole of the validation); `db/init/001_schema.sql:47-51` (the intake columns) and `:76-84` (`kyc_checks`, identity only). |
| **Magnitude** | Applicant-controlled features span roughly 182 score points: `payment_burden` floors at -80 and approaches +42 as DTI approaches zero, `income_sufficiency` runs -20 to +20 across an income/amount ratio of 0 to 5, `employment_tenure` runs -8 to +12. The approve and deny cutoffs are 60 points apart (660 / 600). Income enters twice — as the `payment_burden` denominator and in `income_sufficiency` — so overstating it moves two features at once. |
| **Risk** | **High.** Two distinct exposures. (1) Credit risk: understating `monthly_debt` or overstating `annual_income`/`employment_years` moves the score by more than the entire width of the refer band, with nothing in the platform able to detect it. (2) Reg B accuracy: an adverse-action notice states the specific principal reason for the action (12 CFR 1002.9). Stating "income insufficient for amount of credit requested" against a figure the platform never verified, with no record of that figure's provenance, weakens the notice's evidentiary basis on exactly the ground the D3-era decision-record work was built to strengthen. |
| **Not a claim that stated-income lending is itself the defect** | A stated-income product is a legitimate underwriting choice. The debt is that no such choice was ever made or written down: no verification step, no verification field, no policy statement in `policies/` saying figures are accepted as stated and why. The gap is undocumented and undetectable, not deliberate. |
| **Related dead data** | `applications.employer` and `applications.job_title` (`db/init/001_schema.sql:49-50`) are collected at intake, stored, and returned by officer reads (`services/origination-service/app/routers/applications.py:402,635`), but no scoring path consumes either — `model_vendor` reads only `employment_years`. They are collected-and-unused today. |
| **Attribution** | **Pre-existing.** The intake columns and the absent verification both date to the baseline Halcyon schema; nothing we built removed a check that was there. What our work changed is the stakes: ADR 0009's reason mapping made the platform *name* these three unverified figures in an adverse-action notice, where the prior generic reason string did not. The gap is inherited; the exposure it now creates is partly ours. |
| **Mitigation Path** | Three rungs, independently shippable, smallest first. (a) **Provenance field** — add a per-figure verification flag to the intake record and carry it into `decision_events.inputs`, so the record states whether each scored figure was verified, stated, or unverifiable. Closes the evidentiary half without building any verification capability. (b) **Policy statement** — a `policies/` entry declaring the product accepts stated income and employment, with the compensating controls, so the position is a decision rather than an omission. (c) **Verification capability** — a document store (none exists: no paystub, W-2, bank-statement or upload table anywhere in `db/init/001_schema.sql`) plus a stated-vs-evidence consistency check feeding **refer routing only**, never the score. Rung (c) is the large one and is not scoped. |
| **On putting an agent here** | Raised 2026-08-22 while scoping the agentic freeze work. A stated-vs-evidence consistency classifier is a genuine fit for rung (c) — unbounded document input, a bounded label set (`consistent` / `inconsistent` / `unverifiable`), and a deterministic abstain. It is unbuildable today because there is no document corpus to classify. Two hard constraints if it is ever built: it emits a **refer-routing flag, never a score input or a reason code** — ADR 0009 locks the reason table and `reasons.py` fails closed on any feature it cannot explain, and a model-derived reason would break both; and it must **not** read `employer`, `job_title` or `purpose` free text, which is proxy-rich for protected characteristics and is precisely why ADR 0016 computes fair-lending monitoring outside the platform. |
| **Status** | Open; documented 2026-08-22; not fixed and not scoped into a cycle. Recorded during agentic scoping for the 2026-09-02 freeze, which it does not block — the agentic and trace work touches the officer assistant's narration path, not the scorecard inputs. Priority to be set later. |

---

## Debt sweep 2026-08-22 — new entries

*(Swept while scoping D26. Three findings that were live in the code and absent from this log,
plus one process hole. `D4` is not a new number: service code has cited it since the baseline
and this log never defined it — the same gap D3 had until Week 5. The sweep found no `TODO`,
`FIXME` or `HACK` anywhere under `services/`, and table-level parity between `db/migrations/`
and `db/init/001_schema.sql` holds, so the standing "migrations lag the init DDL" note has no
missing-table instance behind it today.)*

### D27: Passwords are unsalted SHA-256

| Field | Value |
|---|---|
| **ID** | D27 |
| **Finding** | `hash_password` is a bare `hashlib.sha256(password.encode("utf-8")).hexdigest()` — no salt, no key-derivation function, no work factor. Login compares the stored hex to a freshly computed one with `!=`. The schema column comment states the scheme outright. |
| **Location** | `services/gateway/app/auth.py:32` (`hash_password`), `:46` (the comparison), `db/init/001_schema.sql:9` (`password_hash TEXT NOT NULL, -- sha256(password), unsalted`). Declared as a kept brownfield caveat in the `auth.py` module docstring (lines 8–9). |
| **Risk** | **High.** Unsalted single-round SHA-256 over human-chosen passwords is recoverable at GPU speed from precomputed tables — cracking is not the hard part, obtaining the table is. Two properties make it worse than slow hashing: one rainbow table covers every user at once, and identical passwords produce byte-identical hashes, so password reuse across accounts is visible without cracking anything. The affected roles include `admin` and `underwriter`, which are the money-action roles. |
| **Reachability** | Any read of `users` is sufficient. All seven services connect to the one shared Postgres with the same credentials (ADR 0002), so a single service compromise reaches the table; the base compose also publishes 5432 to the host (D21); and D1's credentials remain unrotated. No SQL-injection path is needed for the exposure to matter. |
| **Attribution** | **Pre-existing** (baseline Halcyon auth). Deliberately kept and *documented in the module docstring* — but never entered in this log, so nothing has ever scheduled it. The docstring is the only place it is written down, and a docstring does not appear on a remediation plan. |
| **Mitigation Path** | Argon2id (or bcrypt) with a per-user salt and a tuned work factor, plus **rehash-on-successful-login** so the existing rows drain without a forced org-wide reset: verify against the legacy SHA-256 when the stored value is in the old format, then immediately re-store under the new one, and drop the legacy branch when the last row is migrated. Use `hmac.compare_digest` for the comparison while the legacy branch survives — a non-constant-time compare of a hash is a minor leak, but it is free to close. **Needs a dependency** (`argon2-cffi` or `passlib[bcrypt]`); nothing in the stdlib provides a tuned password KDF beyond `hashlib.scrypt`, which is a usable no-new-dependency fallback. Ask before adding, per the YAGNI rule. |
| **Status** | Open; documented 2026-08-22; not fixed. |

---

### D28: Origination and decisioning use equal nested timeouts, so the outer one can never fire first

| Field | Value |
|---|---|
| **ID** | D28 |
| **Finding** | Origination calls kyc, decision and disclosure through one httpx seam with a module-level `_TIMEOUT = 30.0` applied to every helper. Decision-service's own bureau call uses `timeout=30` as well. The outer budget equals the inner one rather than being strictly smaller, so on a slow bureau the inner call is what expires — origination cannot distinguish "the bureau is slow" from "decision-service is dead", and no deadline is passed down or decremented across the hop. There is no retry or backoff anywhere in origination. |
| **Location** | `services/origination-service/app/clients.py:21` (`_TIMEOUT = 30.0`), applied at `:36`, `:50`, `:57`; `services/decision-service/app/decision.py:78` (`timeout=30` on the bureau pull). |
| **Risk** | **Medium.** An applicant-facing `POST` can block for the full budget on a single downstream stall, and 30 seconds is far past where a submitting borrower has abandoned the form. It compounds the load behaviour ADR 0009 §6 already records and defers — timeouts past roughly 20 concurrent applications on the synchronous chain. It is a latency and diagnosability defect, not a correctness one: nothing is mis-written, and decisioning fails closed. |
| **Retry is not simply missing** | For the bureau pull, no-retry is arguably the correct policy and should be written down as one rather than left as an absence: a credit pull is billable and is a regulated inquiry, so a blind retry duplicates a charge and can duplicate an inquiry on the consumer's file. The gap is that idempotent reads and the paid write share one silent policy. |
| **Attribution** | **Ours, partly.** `clients.py` is the ADR 0004 decomposition seam. Choosing a timeout at all was right; choosing the same number as the downstream, and choosing it once for every call class, is the defect. |
| **Doc drift found alongside** | `CLAUDE.md`'s "Non-obvious facts" section states origination calls downstream "with no timeout/retry contract". The timeout half of that is false — `_TIMEOUT` has been there since the seam was written. Corrected in the same change as this entry. |
| **Mitigation Path** | Give the outer call a budget strictly smaller than the inner one so the caller attributes the stall, or pass an explicit deadline downstream and decrement it. Split connect from read timeouts. State the retry policy per call class — no retry on the paid bureau pull, bounded retry on idempotent reads — rather than leaving one constant to imply it. |
| **Status** | Open; documented 2026-08-22; not fixed. |

---

### D4: The outcome-only `decisions` table is still the one the offer path reads

*(Cited in service code since the baseline — `decision-service/app/models.py:17` and
`origination-service/app/models.py:62` both carry `(debt D4)` — and never defined in this log
until now. Same bookkeeping gap D3 had; this drains one of the collisions the D3 note names.)*

| Field | Value |
|---|---|
| **ID** | D4 |
| **Finding** | `decisions` holds one row per application with nothing but `app_id` and `outcome` — no reasons, no drivers, no inputs, no `decided_by`, no timestamp of the model run. ADR 0009's `decision_events` is the append-only regulated record that fixes this, but `decisions` was not retired: `_persist_event` upserts both in one statement, and several read paths still query the thin table rather than the record. |
| **Location** | `db/init/001_schema.sql:87-91` (the table, with the comment `-- Decision: OUTCOME ONLY. No reason, no model drivers, no inputs, no timestamp of model run.`); written at `services/decision-service/app/decision.py:241-242`. Read at `services/origination-service/app/routers/offers.py:80`, `services/origination-service/app/routers/applications.py:518,641,848`, and `services/decision-service/app/routers/decisions.py:100`. |
| **Risk** | **Medium.** The sharpest instance is `offers.py:80`: offer creation gates on `SELECT outcome FROM decisions`, so the money-adjacent step reads the thin copy rather than the regulated record. Today the two cannot disagree — one atomic statement writes both — but nothing *enforces* that. `decisions` carries no append-only trigger and no constraint tying it to `decision_events`, so a manual `UPDATE`, a repair script, or a future writer that touches only the current-state table would move the offer gate without leaving a record, and the append-only history would still look clean. Same fix-propagation shape as D23, and the same forgeability shape as D20. |
| **Attribution** | **Pre-existing** table; **ours by omission** that it still has readers. The ADR 0009 work added the correct record beside it and left the callers pointed at the old one. |
| **Mitigation Path** | Point the readers at `decision_events` (a current-state view over the latest event per `app_id` keeps the query shape) and reduce `decisions` to a derived projection, or drop it. Until then, an append-only or restricted-write guard on `decisions` closes the forgeability half without touching any caller. Either way it is its own PR. |
| **Status** | Open; entry created 2026-08-22 to reconcile a long-standing code citation; not fixed. |

---

### D29: The set of controls that cannot regress is a hand-maintained allowlist

| Field | Value |
|---|---|
| **ID** | D29 |
| **Finding** | The `backend` matrix runs the whole test suite under `continue-on-error: true` and `python -m pytest -q || true`, so no test in it can fail the build. Everything that must not regress is protected by being *copied out* into its own blocking job, one per control, added by hand. The default for a new control is therefore unprotected, and the failure is silent — the suite stays green either way. |
| **Location** | `.github/workflows/ci.yml:30,33`. `make test` inherits the same posture (each service's invocation ends in `\|\| true`), so the local command cannot fail either. |
| **Risk** | **Medium**, and it is the mechanism behind a shipped Reg Z defect rather than a hypothetical: the add-on-vs-actuarial APR error (4.5pp, roughly 36× the disclosure tolerance, on every loan) survived because the money test that would have caught it ran under `\|\| true`. `CLAUDE.md` names the current instance in its own words — `adjust_balance` and `waive_fee` (D32) are "still inside the suppression, and able to regress on a green build"; `apply_payment` and the red D3 lost-update test left it on 2026-08-24 for the blocking `atomic-apply-gate` (PR #77). |
| **Not an argument for flipping the flag** | The matrix is tolerated for a real reason (known-failing money-math tests that document D2 by design), and turning it blocking today would fail the build on tests that are *supposed* to be red. The debt is the absence of a middle rung, not the presence of the flag. |
| **Attribution** | **Ours.** Every blocking job in the list was added by this program; the allowlist pattern is the program's own convention, and it works — the gap is that it has no backstop when someone forgets. |
| **Mitigation Path** | Make the tolerated set explicit rather than implicit: pin the known-red tests by name (an `xfail` list or a deselect file) and let everything outside that list block. A new control is then protected by default and a newly-red test fails loudly, while the documented D2 failures stay tolerated and, unlike today, stay *enumerated*. Smaller interim step: a check that fails when a test file matching a control's name has no corresponding blocking job. |
| **Status** | Open; documented 2026-08-22; not fixed. Process debt — no code defect of its own. |

---

## Payment-idempotency review round 2026-08-23 — new entry

### D31: Schema-readiness probes for a fresh column are unqualified by schema in three migrations

| Field | Value |
|---|---|
| **ID** | D31 |
| **Finding** | The pattern D19's own migration was just fixed for (a readiness/migration probe against `information_schema.columns` filtered by `table_name`/`column_name` alone, with no `table_schema = current_schema()`) also exists, unfixed, in three earlier migrations, each for a different feature. `information_schema.columns` spans every schema a connection can see, so a same-named table/column in another schema (a decoy, a restored snapshot, a per-tenant schema) is reachable by any of these queries — passing a bad schema or failing a good one, the same failure mode named in the D19 payments-idempotency review round that prompted this entry. |
| **Location** | `db/migrations/0011_applicants_dob_readable.sql:104` (`t.relname = 'applicants'`, no `nspname`/`current_schema()` qualifier on the `pg_class` join); `db/migrations/0013_disclosures_document_body.sql:45-46` (`information_schema.columns` filtered on `table_name = 'disclosures' AND column_name = 'document_body'`); `db/migrations/0014_loans_note_rate.sql:35-36` (`table_name = 'loans' AND column_name = 'note_rate'`). |
| **Trigger** | A database that carries a second schema with a same-named table (`applicants`, `disclosures`, or `loans`) — a staging copy, a restored snapshot, a per-tenant schema — while migrating or checking readiness. |
| **Risk** | **Low.** No reproduction against a real decoy schema for these three specifically (unlike D19's own probe, which was proven this way — see `test_the_guards_read_their_own_schema_not_a_same_named_object_elsewhere`, `services/payment-service/tests/test_idempotency_ddl_live.py`). Scored Low rather than Medium because none of the three guards money movement directly (DOB display, disclosure document body, and the note-rate schedule input are all read-path or disclosure-path, not payment capture), and this repo's docker-compose stack has no per-tenant or decoy schema in normal operation. |
| **Attribution** | **Ours.** Each migration was written by this program (0011, 0013, 0014), and each copied the same unqualified query shape — the same shape D19's own migration 0018 shipped with and a 2026-08-23 review round on `feat/payment-idempotency-capture` (finding M1) then found and fixed there, plus a fourth sibling in `servicing-service/app/config.py`'s `loans.note_rate` readiness probe (same review round). Found while widening context for that fix; not fixed here because migrations 0011/0013/0014 are unrelated features (applicant DOB readability, disclosure document storage, servicing note-rate) and touching them is out of scope for a payments-idempotency PR. |
| **Mitigation Path** | Add `table_schema = current_schema()` (or the `pg_class`/`nspname` equivalent for 0011's `relname` join) to each of the three queries, following the exact fix already applied to migration 0018 and both services' `_payments_idempotency_ready`/`database_reachable` probes. Small, mechanical, one migration file at a time — each is independent and does not need to land together. |
| **Status** | Open; documented 2026-08-23; not fixed. |

---

## Atomic-apply round (ADR 0020) — new entry

### D32: `adjust_balance` and `waive_fee` keep the read-modify-write shape D3 removed from `apply_payment`

*(Carded 2026-08-23 on the ADR 0020 PR, before the D3 fix landed, so the scoping decision was
recorded rather than reconstructed afterwards. `services/servicing-service/app/balance.py` says
these two are "carded in docs/debt-log.md rather than fixed here" — this is that card. Same
bookkeeping gap D3 itself had until Week 5: code citing a register entry that did not exist.)*

| Field | Value |
|---|---|
| **ID** | D32 |
| **Finding** | D3 made `apply_payment` one statement, but the other two mutations of the same `balances` row were deliberately left alone. `adjust_balance` reads the current value, then writes an operator-supplied figure with no predicate on what it read. `waive_fee` reads `past_due`, subtracts in Python, and writes the result back. Neither takes a lock and neither is in a transaction with its own read — the same defect as D3, one and two columns over. |
| **Location** | `services/servicing-service/app/balance.py:160-168` (`adjust_balance`) and `:171-181` (`waive_fee`). Reached via `POST /accounts/{id}/adjust-balance` and `/waive-fee` (`app/main.py:228`, `:245`), both role-gated by `authz.require_money_role`. |
| **Trigger** | Two concurrent mutations of one loan's `balances` row. Two shapes: (a) two operators adjusting, or two waiving, at once — the second overwriting the first; (b) an operator adjustment landing across a concurrent `apply_payment` — the operator's `UPDATE balances SET balance = %s` writes a figure computed before the payment committed, so an applied payment is silently reversed even though the apply itself was atomic. Note the client brief's own example — a payment concurrent with a fee waiver — is **not** this shape: `apply_payment` writes `balance` and `waive_fee` writes `past_due`, so those two do not collide. |
| **Risk** | **Medium**, below D3. Unlike D3 there is no live reproduction for either shape, and neither path is on the borrower-facing payment flow: both are servicer-initiated corrections, staff-paced and low-volume, and `require_money_role` limits who can open the window. The D3 measurement does not transfer — the concurrency that produced D3's $200 (eight retries of one automated intent) is not the shape here. It stays real because (b) can undo a correctly applied payment, and `adjust_balance` is destructive by design (it overwrites the figure and records the prior value nowhere), so with no ledger (D2) and a mutable `audit_logs` (D20) nothing can attribute it afterwards. |
| **Attribution** | **Pre-existing** (baseline servicing), same as D3. Scoped out of ADR 0020 and the D3 fix on purpose rather than missed: the ADR fixes the path where money was measurably lost, and widening it to the correction paths would have grown a breaking cross-service change with no defect behind it. `waive_fee`'s own docstring names a race, though it names `apply_payment` as the counterparty, which is wrong on the columns — the collision is two calls to `waive_fee`. |
| **Mitigation Path** | `waive_fee` takes the same treatment as D3 and is the smaller of the two: `UPDATE balances SET past_due = past_due - :amount WHERE loan_id = :id` computes from the stored value inside the statement. `adjust_balance` is not the same fix — it sets an absolute figure, so it needs the balance it was quoted from as a predicate (a compare-and-set on the value the operator saw, refusing when the row moved underneath), which is a UI-visible behaviour change and needs the client's answer on what an operator sees when it refuses; the deeper question is whether an absolute set should exist at all once `payment_applications` establishes the ledger seam (D2). Do `waive_fee` first; take `adjust_balance` with the append-only ledger (ADR 0014, Proposed, not built), which is what would make either reversible. |
| **Status** | Open; carded 2026-08-23 as a deliberate scope decision on the D3 work. Not started. Pinned, not silent: `test_the_other_mutations_are_still_a_separate_unlocked_read_then_write` and `test_adjust_balance_overwrites_in_place_and_loses_the_prior_value` (`services/servicing-service/tests/test_characterization_balance.py`, added by PR #77) assert today's shape, and the first runs in the blocking `atomic-apply-gate`, so removing the shape turns that gate red and sends whoever does it here. |

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
