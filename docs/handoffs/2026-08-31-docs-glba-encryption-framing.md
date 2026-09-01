# Handoff — D33 (SSN at rest) and D35 (nothing in transit), with the GLBA framing carded (2026-08-31)

**Branch:** `docs/glba-encryption-framing` · **Base:** `origin/main` (`52723c1`) · **Repo:** `/Users/maha/Desktop/revature/MC-meridian-lending`
**Status:** Docs-only change committed, unpushed, no PR. The *build* for D33 has not started and is what this handoff is for.

## What's done

- `21ed47f docs: name the GLBA Safeguards Rule behind D33 and D35` — adds a **Regulatory** row to both entries, corrects D33's mitigation framing and risk ordering, updates both summary-by-severity rows, and fixes a false README compliance claim. Doc gates green (`check_doc_paths`, `check_volatile_claims`, `check_doc_claims`).

**The finding that drove it.** Both entries were carded as breach-risk judgements. There is a rule behind them — **GLBA Safeguards Rule, 16 CFR Part 314 §314.4(c)(3)** (2021 amendments): encrypt customer information *"in transit over external networks and at rest"*. The qualifier splits them:

- **D33 (at rest)** — no qualifier, so plaintext `applicants.ssn` is inside the requirement. Only exception is compensating controls approved **in writing** by the Qualified Individual; nobody has done that. **"Leave it as it is" is not an option the client can pick.** The retention answer selects *which* remediation, not *whether* one happens.
- **D35 (in transit)** — qualified to *external* networks. Every hop is one Docker bridge network on one host, so today's deferral **survives** the rule. It flips at the first hop that crosses a machine — that's now the scheduling trigger instead of "not scheduled".

Nothing prohibits storing a full SSN (FCRA furnishing, TIN reporting, CIP all need it). **The D13a precedent does not transfer**: the CVV deletion needed no client answer because retaining SAD post-authorization is a flat PCI prohibition; there is no equivalent for the SSN.

## What's left

1. **Get the retention answer** (below). It picks the path; both paths are real work.
2. **Build the reversible part of D33 regardless of the answer** — none of this depends on it:
   - `ssn_last4` column, populated at intake (`services/origination-service/app/intake.py:68`)
   - move KYC off the full value — it is `bool(applicant.get("ssn"))` at `services/kyc-service/app/kyc.py:28`, a *presence* check, so last-4 satisfies it identically with no behaviour change
   - leave the bureau pull reading the full value while one exists
   - a purge mechanism that is **fail-closed, window-configurable, and inert until switched on**
   - readiness rung + a blocking gate, per the house rules
3. **Do not run the back-book purge on an assumption.** That is the one irreversible step and needs an explicit human yes.
4. Consider **ADR 0023** (next free; 0022 is the offline evaluator). Three options with real trade-offs — this is ADR-shaped by the project's own rules.

### One design question inside the slice

The slice above says `ssn_last4`, but KYC's actual need is `bool(applicant.get("ssn"))` — **a boolean satisfies it**. Last-4 is only required if a *human* (a CSR verifying a caller) needs it. If not, a presence flag is strictly less PII for identical functionality, and last-4 beside a name and DOB is not nothing. Decide this before adding the column; it is cheaper than removing it later.

## The rest of D33 — beyond the reversible slice

The slice plus one yes/no is **not** the whole job. Four more pieces, and the first has the real unknowns in it.

### 1. The purge is not migration 0020's shape

0020 is the right precedent for *technique* and the wrong one for *shape*. It dropped a column, once. D33 cannot: the bureau pull needs the real digits while an application is decisionable, so the column stays and the **value** is nulled at terminal state. That makes the purge **recurring and event-driven, not a one-shot migration**, and it moves the hard part:

- Every purge leaves the old row versions as dead tuples. 0020 answered that with one `VACUUM FULL`. That cannot run on every purge — it takes an `ACCESS EXCLUSIVE` lock on `applicants`, blocking intake and every officer read.
- So the open question is whether routine autovacuum reclaims those tuples on an acceptable schedule, or whether scheduled maintenance is required. **Until that is answered, "purged" means "nulled in the live row, still readable in the data files for an unbounded window"** — which is not what the entry would be claiming.

Resolve this before writing the migration, not after.

### 2. The encryption branch is a second build, not an addition

If the retention answer is "retain through the record-retention window", the purge path is off and the work becomes application-level encryption: a key from a secret manager, a rotation and re-encryption story, and both read sites changed. That waits on a deployment existing — the same blocker as D35. It is largely *instead of* the slice, not on top of it.

### 3. Copies the purge cannot reach

The same **Not covered** row D13a still carries applies here identically:

- pre-redaction log files — D5's open residual, explicitly "flagged here but not audited"
- database backups and WAL segments, and any replica

Nulling the column does not remove the SSN from the system. That needs its own retention action, which nobody has taken for the PAN/CVV either. **Closing D33 without saying this would claim more coverage than the code has** — the failure mode the debt-log status vocabulary exists to prevent. Name it as a residual on the entry.

### 4. The browser hop still sends it in the clear

`frontend/app/apply/page.tsx:352` posts the raw SSN. The field is a password input with a reveal toggle (`:198`), which is shoulder-surfing protection, not transport. That hop is plaintext `http` today — D35. At-rest work does not touch it.

## Verified: no work needed here

Checked so the next session does not re-investigate:

- **No response surface echoes the SSN.** `services/origination-service/app/routers/applications.py:91` and `:611` are internal forwards to kyc-service and decision-service; `services/decision-service/app/schemas.py:12` is the *request* model, not a response. Nothing returns it to a browser.
- `decision_events` is already identifier-free — peppered HMAC at `services/decision-service/app/decision.py:139`.
- The LLM path masks it before the model boundary (`services/origination-service/app/llm/request_builder.py`).
- Intake keeps writing the full value under either retention answer.

## Blockers / open questions

Two, both for the client (Priya, Compliance Officer; Dana, VP Lending Ops):

1. **How long after submission must the platform be able to re-run a bureau pull?** This is the whole dependency. *Not once the decision is final* → purge to last-4 at terminal state, no key management, risk removed. *Any time in the retention window* → the column persists and **must** be encrypted, which needs a key-management answer, which waits on a deployment existing. Check against the record-retention rule (ECOA/Reg B, 25 months, 12 CFR 1002.12) — whether the *raw* SSN is in scope is counsel's read, not ours.
2. **Does anything outside this repo need the SSN** — servicing back office, collections, year-end reporting? Verified *inside* the repo: nothing reads it post-boarding, and `loans` has no SSN column. Only the external boundary is unknown, which is why the purge should ship as a switch, not a one-shot migration.

## Key files

- `db/init/001_schema.sql:24` — `ssn TEXT, -- plaintext`. The only place in the repo that records the fact.
- `services/origination-service/app/routers/applications.py:208` and `:585` — the **only two reads** of the stored column. `:208` feeds the CIP re-check, `:585` forwards to decision-service.
- `services/kyc-service/app/kyc.py:28` — presence check, not verification. Does not need the value.
- `services/decision-service/app/decision.py:330` — `_pull_credit(application.get("ssn", ""))`. The **sole** consumer of the real digits.
- `services/decision-service/app/decision.py:139` + `app/config.py` — the peppered-HMAC pattern to mirror; it already refuses a placeholder pepper and reports unhealthy outside development.
- `db/migrations/0020_payments_drop_cvv.sql` — the purge shape to copy: `UPDATE` + `DROP` + assert + `VACUUM FULL`, guarded statement-by-statement so it is re-runnable. A `DROP COLUMN` without the rewrite leaves every value in dead tuples.

## How to verify / run

```bash
./scripts/check_doc_paths.sh && ./scripts/check_volatile_claims.sh && ./scripts/check_doc_claims.sh
```

All three green as of `21ed47f`. No code changed, so no service suite or gate applies to this commit.

## Branch state

- `main` (`52723c1`) = the client's real state: `applicants.ssn` plaintext, `payments.pan` plaintext (D13b), nothing encrypted at rest or in transit anywhere in the codebase. **There is no encryption in this repository at all** — what exists is keyed hashing (continuation tokens, the SSN fingerprint, PBKDF2 passwords) and masking.
- `docs/glba-encryption-framing` = this commit, docs only, no behaviour change.
- Closed since the sweep that carded these: **D21a/D21b** (datastore ports, merged), **D20** (`audit_logs` append-only triggers), **D27** (PBKDF2 passwords). Remaining from that sweep: **D13b**, **D33**, **D34**, **D35**, plus the unencrypted-backups residual inside **D5**.

## Debt log refs

- **D33** — Open, not started, nothing blocking holds it. Now first in priority among what is left.
- **D35** — Open, deferral now conditioned on topology rather than left open-ended.
- **D34** — Open; unpeppered `sha256(pan)` fingerprint, to be fixed *inside* D13b.
- **D13b**, **D5** (backup/WAL residual) — unchanged.

## ⚠️ Process warning for the next session

**This branch's edits were destroyed once already.** A parallel Claude Code session ran a reset in the shared checkout while these files were modified-but-unstaged, then switched branches:

```
52723c1 HEAD@{1}: reset: moving to HEAD    ← discarded unstaged work
```

Unstaged changes leave no object in the store, so there was nothing to recover — they had to be retyped. **Work this branch from a dedicated worktree, and commit early**, e.g.

```bash
git worktree add <scratch>/wt-glba docs/glba-encryption-framing
```

Twelve stashes and fourteen worktrees are live; several sessions are moving `HEAD` in the primary checkout. Check `git log -3` before any amend or reset, and recover additively rather than force-pushing.

## Next session: start here

Ask Dana/Priya question 1 (bureau re-pull window). While waiting, build the reversible slice in step 2 above on a fresh `fix/ssn-at-rest` cut from `main` — none of it depends on the answer, and it is the whole change minus the one irreversible step.
