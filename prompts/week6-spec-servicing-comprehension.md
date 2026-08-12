# Spec: Week 6 servicing money-controls comprehension report + tests

Self-contained. Run Explore → Plan → Implement → Commit + PR. PR is the unit of delivery.

## Context

Client ask (Dana, VP Lending Ops): servicing dashboard so reps can adjust balances / waive fees.
**This PR does NOT build the dashboard.** It is the comprehension groundwork the client brief
requires before any build: an AI-augmented legacy-comprehension report on the servicing money
surface, characterization tests pinning current behavior, and a failing test proving the
concurrent lost-update. A companion ADR (separate PR, separate branch) proposes the fix; do not
change any endpoint/authz/schema code in this PR.

Branch from `main`: `docs/servicing-comprehension-week6`.

## Checkpoints — expected answers, not evidence (verify each in code first, then cite)

These are the conclusions a prior dig reached. They are **hints to check against**, not findings
to copy. For each bullet: open the cited file:line, confirm the claim still holds, and write the
report from what you read — citing the line you actually opened. If a checkpoint disagrees with
the code, the code wins: say so in the report and correct the citation. A report that restates
this section without opening the files has done none of the work.

- Money endpoints accept ANY authenticated caller. `adjust_balance` / `waive_fee` *declare*
  `x_user_role` (`services/servicing-service/app/main.py:103,114`) but never read it. Zero
  approvers.
- Lost update: `services/servicing-service/app/balance.py:23-32` = `SELECT balance` then
  `UPDATE balance` as two separate autocommit round-trips (`db.py:13` `autocommit=True`); no
  `FOR UPDATE`, no version column, no transaction.
- No ledger: `balances` = one mutable `DOUBLE PRECISION` column (`db/init/001_schema.sql:117-122`).
  `payments` rows don't link to deltas. `audit_logs` mutable + soft-delete, never written by
  balance code. Only true append-only table in this schema = `decision_events`
  (`001_schema.sql:148-179`).
- `apply_payment` has TWO callers: HTTP route (`main.py:84`) + in-process `payments.py:79`.
- Gateway forwards authoritative `X-User-Role` / `X-User-Id` (`services/gateway/app/main.py:171`);
  servicing reads neither.
- Debt log ties: **D8** (servicing enforces no authz, Critical), **D2** (float everywhere incl.
  balances, no ledger), **D20** (`audit_logs` mutable). See `docs/debt-log.md`.

**Correction to make explicit in the report:** the client brief's literal example (concurrent
payment + fee waiver landing on the wrong final number) does NOT reproduce a lost update —
`apply_payment` writes the `balance` column (`balance.py:27-30`), `waive_fee` writes `past_due`
(`:52-53`); different columns, no collision. The real lost update is **same-column** concurrency:
two `apply_payment` (or `apply_payment` + `adjust_balance`) both read `balance=500`, both write —
final = last writer, first delta lost (500-100-200 should be 200; comes out 300). Root cause:
non-atomic read-modify-write (`balance.py:23-32`), no `FOR UPDATE`, no version, autocommit per
statement (`db.py:13`).

## Report findings to answer

Same rule as the checkpoints above: each "Answer" below is the expected conclusion, not the
deliverable. Reach it from the code yourself and cite what you opened; flag any that no longer
holds.

**Q1 — Who can move money / waive a fee? Is there a second approver?**
Answer: nobody is gated. Gateway gates auth only, no role — `_require_user`
(`services/gateway/app/main.py:193-197`); `/lss/*` (`:421-424`) and `/payments` (`:457-463`)
call only it.
Servicing declares `x_user_role` on `adjust_balance`/`waive_fee` but never inspects it
(`balance.py:35-56` take no caller identity). All four demo roles including borrower `maria` can
hit these on any `loan_id` (IDOR). Ties to debt **D8** (Critical).

**Q2 — Does the concurrent payment + waiver example land on the correct final number?**
Answer: the brief's literal pair doesn't collide (see correction above). Explain the real
same-column race and what triggers it.

**Q3 — Can you reconstruct account 7781's balance history?**
Answer: no. `balances` (`001_schema.sql:117-122`) stores current value only, `updated_at`
overwritten each write, no actor/delta/prior-value/reason. No servicing code writes `audit_logs`
(grep to confirm) and that table is mutable + soft-deletable anyway (**D20**). `payments` rows
exist only for the card-charge path (`payments.py:75`); manual `adjust-balance`/`waive-fee` and
the split-flow `apply-payment` persist nothing beyond the `UPDATE balances` + an ephemeral
`log.info`. State the answerable depth today (current balance, current past_due, last-modified
timestamp — nothing else) and what an append-only ledger would need to add.

**Q4 — Which actions are money-affecting; which need maker-checker + a ledger row?**
Produce a table over `services/servicing-service/app/main.py` endpoints: `POST /payments`,
`POST /apply-payment`, `POST /adjust-balance`, `POST /waive-fee`, `POST /late-fee`, and reads.
Columns: effect, needs-ledger (every mutation: yes), needs-maker-checker (only `adjust-balance`
and `waive-fee` — arbitrary/discretionary moves; `apply-payment`/`late-fee` stay automated —
system-driven idempotency and rule-driven, not discretionary), RBAC expectation. State the
principle: every balance/past_due mutation writes a ledger row; maker-checker binds only manual
discretionary moves.

## Deliverables (this PR only)

1. `docs/servicing-money-comprehension-week6.md` — the report. Sections: money-endpoint
   inventory (table, file:line refs), balance-mutation walkthrough (the read-modify-write), the
   four questions above with answers, the Q4 table, D8/D2/D20 tie-in, and a short "what the
   companion ADR (docs/servicing-money-controls-adr-week6 branch) decides" pointer — don't
   pre-decide RBAC/ledger/money-type mechanics here, that's the ADR's job.
2. `services/servicing-service/tests/test_characterization_balance.py` — golden-master tests
   pinning CURRENT behavior: `adjust_balance` overwrites in place, `waive_fee` subtracts
   `past_due`, `apply_payment` subtracts `balance`, role header accepted-but-ignored. Mirror the
   existing test idiom in `services/servicing-service/tests/` (e.g. `test_money.py`,
   `test_schedule.py`) — same fixtures/DB setup pattern, don't invent a new harness.
3. `services/servicing-service/tests/test_lost_update.py` — the failing test. Two concurrent
   `apply_payment` calls on one loan, SAME column (`balance` — per the Q2 correction, the brief's
   payment+waiver pair does not collide), separate connections, interleaved
   read→read→write→write, assert the final balance is wrong. This test is meant to stay red — it
   documents the defect, it is not a `make prove` red/green pair, and no fix ships this week.
   Mark it intentionally-failing the way this service already marks one: a module docstring
   stating it currently FAILS and why, and **no** `xfail`/`skip` marker — `test_money.py` uses
   exactly that shape, and this service has no `pytest.ini`/`pyproject.toml` to configure markers
   in. A real red is the deliverable; an `xfail` would report green and hide the defect. Add it
   to the tolerated-red matrix in Verification below.

## Explicitly out of scope (say so in the report, and don't touch)

No dashboard build. No endpoint/authz code change. No ledger DDL or migration. No `balance.py`
fix. Those land later, driven by the companion ADR.

## Critical files to read first (reuse, don't reinvent)

- `services/servicing-service/app/main.py`, `app/balance.py`, `app/payments.py`, `app/db.py`
- `services/servicing-service/tests/` (existing test idiom — copy its shape)
- `services/gateway/app/main.py` (auth/role forwarding, lines cited above)
- `db/init/001_schema.sql` (`balances`, `payments`, `audit_logs`, `decision_events` DDL)
- `docs/debt-log.md` (D8, D2, D20 — quote, don't paraphrase from memory)

## Verification before opening the PR

**Tolerated-red matrix.** A bare `python -m pytest -q` in this service exits 1 *before* this PR:
`tests/test_money.py::test_float_payment_drift` is intentionally failing (its module docstring
says so; the CI `backend` matrix runs servicing under `continue-on-error` + `|| true`). This PR
adds a second intentionally-failing test. So never grade the suite on its exit code — run the
green set and the red set as separate commands and check each against its expected result:

| Command (from `services/servicing-service`) | Expected |
|---|---|
| `python -m pytest -q --ignore=tests/test_money.py --ignore=tests/test_lost_update.py` | **all pass**, exit 0 — includes the new `test_characterization_balance.py` |
| `python -m pytest -q tests/test_characterization_balance.py` | **all pass**, exit 0 |
| `python -m pytest -q tests/test_lost_update.py` | **fails red as designed**, exit 1 — the deliverable, not a regression |
| `python -m pytest -q tests/test_money.py` | **fails red, pre-existing** — unchanged by this PR, do not "fix" it |

- Run `python -m pytest -q tests/test_lost_update.py` several times (5+) and confirm it fails
  every run — it must reproduce the race reliably, not flake. A run that passes means the test
  does not force the interleaving and needs rewriting.
- Record this matrix in the report so a reader who runs the bare suite knows which two failures
  are expected and why.
- Every file:line reference in the report resolves in this branch's tree. If the report gets
  backticked paths, run `./scripts/check_doc_paths.sh` (matches the `doc-path-lint` CI gate).
- Diff stays under ~400 changed lines (W7-10 size rule).
- Do not touch `adr/`, migrations, or any `.py` outside the two new test files.
