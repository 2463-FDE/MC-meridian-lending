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
| | *(Literal values intentionally not reproduced here — see `docs/security-remediation-2026-07.md`. They are purged from source on `security/purge-committed-secrets` and must be rotated.)* |
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
| **Status** | Open; flagged as debt; accepted design risk (tradeoff: simplicity vs. correctness). |

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
| **Status** | Open. **Planned:** redaction strategy is designed in ADR 0006; the `PiiRedactor` code + tests land in a separate PR (`feature/pii-redaction`) and are not part of this docs branch. Not marked fixed until that code + tests are merged. |

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
| **Status** | Open; **documented as debt; no fix in scope for Week 1.** Will block production deployment until addressed. |

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
`73ef737`) was **fixed** on `fix/redactor-ssn-separator-blindspots` — see D22. Everything else is
logged here as pre-existing debt; not fixed, out of scope for "parts we touched".

### D8: Servicing service enforces NO authorization (IDOR + no maker-checker)

| Field | Value |
|---|---|
| **ID** | D8 (referenced in `servicing-service/app/main.py` comments; entry created 2026-07-19) |
| **Finding** | `servicing-service` has no authz module at all. Gateway `/lss/*` and `/payments` proxies do session-auth only (no role check). Two consequences: **(a) IDOR** — any authenticated user reads or mutates any loan by walking serial ids; **(b) no role/maker-checker** — a borrower can move money on any account. |
| **Location** | Reads: `servicing-service/app/routers/loans.py` (`GET /loans/{id}` L55–66, `GET /loans/{id}/payments` L78–91), `main.py` (`GET /accounts/{id}/balance` L88–94). Mutations: `main.py` `adjust-balance` L101–105, `waive-fee` L112–116, `apply-payment` L79–85. Gateway: `main.py` `/lss` L421–425, `/payments` L457–464. |
| **Trigger** | Log in as borrower `maria`; `GET /lss/loans/{1,2,3…}` enumerates every borrower's loan/balance/payment history; `POST /lss/accounts/1/adjust-balance {"new_balance":0}` zeroes a stranger's balance; `.../waive-fee` waives any fee. All succeed. |
| **Risk** | **Critical.** Cross-customer PII disclosure + unauthenticated-in-effect money mutation. |
| **Attribution** | **Pre-existing** (baseline `d59f331` servicing). Related to **ADR 0010** (officer-or-owner authz), which we implemented on origination only and explicitly deferred for servicing pending an applicant identity/signup flow that does not exist. Not our feature's code; the deferral is documented in ADR 0010. |
| **Mitigation Path** | Extend ADR-0010 `require_officer_or_owner` (plus a role gate + second-approver on money moves) to servicing. Needs the identity flow ADR 0010 is blocked on. Own PR/ADR. |
| **Status** | Open; documented; **not fixed (parts-we-touched scope excludes servicing).** Blocks production. |

### D19: Payment charge has no idempotency key (double-charge on retry)

| Field | Value |
|---|---|
| **ID** | D19 |
| **Finding** | The `payments` table has no idempotency key / unique charge reference, and `charge()` inserts a row then calls `apply_payment` with no dedupe. A retried/double-submitted `POST /payments` inserts a second row and debits the balance twice. |
| **Location** | `db/init/001_schema.sql` payments DDL ("no idempotency_key, no unique(charge_ref)"); `payment-service/app/payments.py` L74–79 and `servicing-service/app/payments.py` L74–77; call site `main.py` L68. |
| **Trigger** | Client timeout + retry, or double-click, on a card charge → two `payments` rows, balance debited twice, no key to collapse them. |
| **Risk** | **High.** Duplicate customer charges; compounded by D2 (no ledger to reconstruct). |
| **Attribution** | **Pre-existing** (baseline `60d1c37` servicing/payments). Not touched by our features. |
| **Mitigation Path** | Add `idempotency_key` column + unique index; require callers to pass one; replay the prior result on conflict (same pattern our decision/offer/loan boarding already use). Own PR. |
| **Status** | Open; documented; not fixed (out of scope). |

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
| **Status** | **Fixed** on `fix/redactor-ssn-separator-blindspots` (77 redactor tests pass). |

---

## Summary by Severity

| Severity | Finding | Status | Week 1 Action |
|---|---|---|---|
| **Critical** | D1: Hardcoded credentials | Open | Document, flag, schedule rotation (Week 2+). |
| **Critical** | D5: Plaintext PII in logs | Open | **Planned: ADR 0006 designs redaction; code + tests in `feature/pii-redaction` PR (not yet merged).** |
| **Critical** | D13: PAN/CVV in DB | Open | Document, flag, schedule tokenization (Week 2–3). |
| **High** | D2: Float money math | Open | Document, flag, schedule migration to Decimal (Week 2+). |
| **Medium** | D14: Encoded PII bypasses log redaction | Deferred | The log redactor matches literal shapes only, so percent-encoded (email=maria%40example.com, ssn=412%2D55%2D9981) and unicode-escaped (@) PII in uvicorn access-log query strings is not masked. Payload vector closed by allowlist logging; no sensitive route accepts PII via query/path today, so exposure is a client-crafted query param. Follow-up: bounded URL-decode + \uXXXX-unescape normalization pass in the (CI-synced) redactor, with regression tests for encoded email/SSN/phone. Not done now to avoid a byte-altering change to the shared redactor for a low-exposure case. |
| **Low** | D15: `redactor.py` duplicated per service (no shared package) | Mitigated | `services/*/app/redactor.py` is a near-identical copy in each of the 7 services — no shared module. Drift risk (a fix in one not reaching the others) is held closed by the **blocking** `redactor-drift` CI job, which fails the build if any copy diverges from the canonical; copies are resynced with `scripts/sync_redactor.sh`, never hand-edited. So this is a maintainability/structure cost, not an open leak path. Follow-up: extract a shared internal package (e.g. `libs/redaction`) so the copies collapse to one import; deferred because the CI gate already prevents divergence and a shared package adds packaging/build wiring across 7 services (YAGNI until a 2nd shared util appears). |
| **Low** | D17: Offer-replay schedule APR default 0 vs 7.99 | Open (residual) | Null-APR offer row would disclose 0% APR beside a 7.99%-computed schedule (`_offer_response_from_persisted`). Not reachable — `accept_offer` rejects null-APR, `build_offer` always writes one. Display-only; unify the default when next touched. |
| **Low** | D18: Fresh-insert vs replay offer schedule cent drift | Open (residual) | Retry returns a schedule reconstructed from the stored disclosure box (principal backed out via `amount_financed/0.97`, term via `round(total/monthly)`); float round-trip can drift a cent per row vs the fresh-insert response. Disclosure totals unaffected. Subsumed by D2. |
| **Low** | D16: RAG eval index is in-memory only (pgvector deferred) | Deferred (by design) | The `rag_eval` harness rebuilds an in-memory exact-cosine index each run and keeps no persistent vector store (ADR 0007 rule 6). Correct at the current 9-chunk corpus — brute-force cosine is microseconds and a vector DB would add latency for zero benefit. **Phase 2 trigger — build a `PgVectorIndex` behind the existing `Index` contract (`add`/`search`/`__len__`) when ANY holds:** (1) corpus grows past ~hundreds of chunks (brute-force starts to hurt); (2) vectors must persist/share across runs or processes; (3) more than one service queries the same vectors. Scoped in `docs/PHASE1-BEDROCK-PGVECTOR.md` § Phase 2. When triggered it is **its own PR**: `pgvector/pgvector:pg16` image, `CREATE EXTENSION vector` + schema migration, DB wiring into the (currently DB-free) harness, and an **ADR 0007 rule 6 amendment** with a PII re-review of the persistent store. Not started; the `Index` seam is the readiness, so no code carries the cost until then. The Bedrock embedding backend (Phase 1) is already built + smoke-tested and is independent of this. |
| **Critical** | D8: Servicing enforces no authz (IDOR + no maker-checker) | Open (pre-existing) | Teeth 2026-07-19. Any authenticated user reads/mutates any loan (serial-id IDOR) and moves money on any account — servicing has no authz module. Related to ADR 0010 (implemented origination-only, servicing deferred pending identity). Not our feature's code; own PR/ADR. |
| **High** | D19: Payment double-charge (no idempotency key) | Open (pre-existing) | Teeth 2026-07-19. Retried `POST /payments` inserts a 2nd row + debits balance twice; no idempotency key / unique charge ref. Add key + unique index + replay-on-conflict. Own PR. |
| **High** | D20: `audit_logs` mutable + seeded plaintext PAN | Open (pre-existing) | Teeth 2026-07-19. No append-only trigger (has `deleted_at`), rows UPDATE/DELETE-able; seed writes raw PAN into `detail`. Forgeable "audit" contradicts README SOX claim. Add append-only trigger + scrub seed. Own PR. |
| **Medium** | D21: Postgres/Redis host-published in base compose | Open (pre-existing) | Teeth 2026-07-19. `5432`/`6379` published to host bypass app-layer authz. Drop `ports:` (keep `expose`). Own PR. |
| **Medium** | D22: Redactor missed unlabeled dot/slash/tab/multi-space SSN | **Fixed** | Teeth 2026-07-19. **The one finding in code our features introduced** (Week-1 redactor). Generalized `3a-bis` + widened `3b`; resynced 7 copies; regression tests added. Fixed on `fix/redactor-ssn-separator-blindspots`. |

---

## Week 1 Actions

✓ **D1:** Documented; flagged for rotation (Week 2).  
✓ **D2:** Documented; flagged for Decimal migration (Week 2+).  
◻ **D5:** **Planned (ADR 0006).** Redaction strategy designed; `PiiRedactor` code + tests are in a separate PR (`feature/pii-redaction`) and not yet merged. Existing logs flagged (not deleted, out of scope). Not fixed until that code + tests land.  
✓ **D13:** Documented; flagged for tokenization (Week 2–3).  

---

## Next Steps

- **Week 1:** Complete logging redaction (D5) and verify via integration tests.
- **Week 2:** Rotate credentials (D1), begin tokenization design (D13).
- **Week 2–3:** Migrate to Decimal for money math (D2).
- **Ongoing:** Review new debt as it emerges; update this log.
