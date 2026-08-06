# Build Invariants

Correct-by-construction catalogs. Sibling to `docs/review-roundtrip-playbook.md` — that
one makes the adversarial reviewer converge in 2–3 rounds; **this one stops the finding
from being writable in the first place.**

Derived from PR 7 (`feature/decision-assistant-week3`): ~70 fix commits after push, most
adding *one* property of a subsystem per round. The properties were knowable at design
time. When a feature matches a **kind** below, paste that kind's matrix into the spec's
*Acceptance Criteria › Security/Compliance* and make every cell a test **before** coding.
The ADR then states which invariants it discharges (see *Wiring*).

## How to use

1. At spec time, tag the feature with every **kind** it touches (a feature can be several).
2. Paste the matching matrix into the spec acceptance criteria. Empty cell = unbuilt work.
3. Write the cell's test with the feature, not after the bot finds it.
4. In the ADR, add a line: `Discharges build-invariants: <kinds>`. A cell you deliberately
   defer is fenced in the ADR *Consequences* with a tracking ref — never left silent.

Kinds catalogued here: **ephemeral-credential**, **dual-store-atomicity**,
**replay-determinism**, **crypto-material**. Idempotency / authz / new-field / filter-bypass
already live in the review-roundtrip playbook's chain-completion rules — cross-referenced,
not duplicated.

---

## Kind: ephemeral-credential

Any feature that mints a token, session id, resume code, or one-time credential.

> PR 7 hardened ONE continuation-token subsystem across ~13 serial rounds — hash-at-rest,
> TTL, versioned pepper, HttpOnly cookie, server-side session, clear-on-funding,
> cascade-PII, non-ASCII 500. Every row below was a separate review round.

| Axis | Invariant | PR 7 leak it would have closed |
|------|-----------|-------------------------------|
| At rest | Stored **hashed**, never plaintext | `81ce949` hash at rest |
| At rest | Keyed with a **dedicated** secret, never a public/placeholder/shared pepper | `e47807b`, `296269f` |
| At rest | Pepper/key is **versioned**; the prior version stays decodable during rotation | `81b0591` reserve legacy version |
| Expiry | Has a **TTL**; expires server-side | `81ce949` TTL |
| Expiry | **Cleared on terminal state** (funded / accepted / abandoned) | `81ce949` clear on funding |
| Transport | Server-side session behind an **HttpOnly** cookie, not a body/URL field | `ffa9cf9`, `d036b84` |
| Transport | Scoped to the one flow that issues it (anonymous ≠ officer) | `cc5e2bc` scope to anon flow |
| PII coupling | Abandon/expiry **cascades** the PII the credential guarded | `296269f` cascade abandon PII |
| Malformed | Compared as **bytes**; non-ASCII/garbage → 4xx, never 500 | `0b87f1d` bytes compare |
| Concurrency | Raced abandon / double-use → **409**, not a stale grant | `81b0591` raced abandon 409 |

**Build-time shortcut:** implement issue/verify/expire **once** as a shared helper. Ten of
these rows are free if the second caller reuses the first caller's primitive.

---

## Kind: dual-store-atomicity

Any write that touches **two** stores in one logical operation (here: Postgres + Redis).

> PR 7: `efdb424` atomic-against-Redis-outage, `32d9068` compensating rollback on stranded
> submit, `cc5e2bc` Redis blip on read, `de6f8db` atomic submit + readiness.

Decide these three at design time, in the ADR, before the code exists:

1. **Write order.** Which store is the source of truth; the other is derived/cache.
   Commit truth first, derive second.
2. **Compensating action.** If store-B write fails after store-A commits, what undoes or
   quarantines store-A? Name it. A "stranded" row with no compensation is the default bug.
3. **Store-B outage path.** Read: fail closed or serve stale? Write: reject (4xx) or queue?
   The outage path is a spec line, not an afterthought — `efdb424` was the whole submit
   flow re-architected in review because the outage path wasn't designed.

| Cell | Question | Fail state if unanswered |
|------|----------|--------------------------|
| Order | Truth store committed before cache? | Cache points at nonexistent truth |
| Rollback | B fails post-A-commit → compensate? | Stranded row (`32d9068`) |
| Read outage | B down on read → closed or stale? | 500 on Redis blip (`cc5e2bc`) |
| Write outage | B down on write → 4xx or queue? | Silent half-write (`efdb424`) |
| Race | Concurrent op on same key serialized? | Double-fire (`2940221`) |

---

## Kind: replay-determinism

Any idempotent / cached / replayed response.

> `24a1a34` replay offers from persisted row not recomputed terms · `714a444` build
> decision summary from record not model · `9c25567` reconcile balance on replay.

**One rule:** a replayed response is **reconstructed from the persisted source of truth,
never recomputed.** Recompute drifts (float math, changed policy, model nondeterminism);
the stored row does not. Distinct from the playbook's stale-replay-409 cell — that guards
*writes*, this guards *reads*.

Cells: (a) replay reads the stored row; (b) no recompute path exists on the replay branch;
(c) derived state (balance, funded flag) reconciled from the row on every replay.

---

## Kind: crypto-material

Any pepper, HMAC key, or signing secret.

- Never a public constant, `.env.example` placeholder, or empty-string default (`e47807b`).
- **Versioned** at rest so rotation doesn't strand existing tokens (`81b0591`).
- Reject-any-non-empty is not validation — check it is not the placeholder (playbook trap 3).

---

## Structural shift-left (kills the class, not the cell)

The catalogs above are per-feature discipline. These three remove whole classes at the
architecture level — worth more than any checklist:

1. **Guarantee = one shared primitive.** Idempotency is hand-rolled in decision, servicing,
   payment, and origination separately — no common package (same root as the 7× duplicated
   `redactor.py`). Each copy leaks in review independently. A single `@idempotent(key, scope)`
   applied once makes the parity-sweep class un-writable.
2. **Invariant in the schema, not the code.** `e0716da` was a unique index that existed in a
   migration but not the canonical init DDL — a build-artifact split, not a logic bug.
   `NOT NULL` + `CHECK` beats quarantine-then-remediate-in-code (the `monthly_debt` ladder
   cost 7 rounds). If the DB enforces it, no route can code around it.
3. **Gateway deny-by-default.** Anonymous-forward and verb-reachability leaks come from the
   permissive proxy passthrough. Forward nothing unless the route declares
   `{verbs, auth, ownership}`. Removes the front-door + authz-forward classes structurally.

---

## Wiring

- **Spec:** paste each applicable kind's matrix into *Acceptance Criteria › Security/Compliance*.
- **ADR:** add `Discharges build-invariants: <kinds>`; fence deferred cells in *Consequences*
  with a tracking ref.
- **teeth:** Phase 2.5 gauntlet already probes these dimensions in review — this doc moves
  them upstream to design so teeth finds nothing new.
- Indexed in `docs/kb.md`.
