# ADR 0017: Compensating Control for Anonymously-Submitted Self-Decisioned Applications

- **Status:** **Proposed** — not built. This ADR records the decision; implementation is a
  separate PR pending confirmation of the open questions in Sign-off status.
- **Date:** 2026-08-14
- **Author:** Claude Code
- **Related:** ADR 0010 (application ownership authorization — the anonymous-apply design
  this gap is inherent to). Debt D24 (`docs/debt-log.md`, merged to `main` in PR #38) — the
  self-decision segregation-of-duties control this ADR extends. The governance ask that
  produced it (2026-08-12 §5).
- **Source:** PR #38 review, rounds 1–4, against `fix/self-decision-authz`.

Numbering note: `db/migrations/0017_applications_submitted_by_user_id.sql` (on
`fix/self-decision-authz`) and this ADR share the number `0017` by coincidence — migrations
and ADRs are numbered in separate sequences (`db/migrations/` vs `adr/`). They are related but
distinct artifacts.

---

## Context

The governance ask (2026-08-12 §5) requires that no officer decision their own credit
application: "block the decision route when the caller and the applicant are the same
account... log the blocked attempts." This is a segregation-of-duties control — the platform
must not let staff act as underwriter on their own file.

`deny_self_decision` (`services/origination-service/app/authz.py`, on `main` since PR #38)
closes this for two identifiable cases: an officer whose login is linked to the applicant
record (`users.applicant_id == applications.applicant_id`), and an officer who submitted the
application while authenticated (`applications.submitted_by_user_id`, captured from
`X-User-Id` at intake). PR #38 review (rounds 2 and 3) drove both checks into the code.

Neither check can see a third case. `POST /applications` is anonymous by design (ADR 0010
Phase B) — the platform's primary intake channel exists specifically so a borrower can apply
without creating an account first. An application submitted this way carries
`submitted_by_user_id = NULL`, and that is not a transitional state: it is the permanent,
correct value for every genuinely anonymous application, before this fix and after it. An
officer can submit their own application through this exact channel while logged out, then
log in as underwriter or admin and decision it. The row that results is, at the SQL level,
indistinguishable from a real borrower's anonymous application. PR #38 round 4 names this
correctly: it is a live, ongoing exposure through the platform's normal anonymous-apply path,
not a historical artifact that a migration or a backfill could close.

The question this ADR answers: given that the primary intake channel must stay open to
genuinely anonymous applicants, and given that the two existing checks provably cannot see
this case, what is the appropriate compensating control?

---

## Decision

We will require a second, different officer's explicit approval before a decision reaches
`decision-service`, but only when the application is a plausible match for the deciding
officer — narrowing the added friction to the slice of anonymous applications actually worth a
second look, not the whole channel.

**Trigger condition (both must hold):**
1. `applications.submitted_by_user_id IS NULL` (no authenticated submitter captured — the
   anonymous-apply case check 2 cannot see).
2. The deciding officer's own `users.display_name` and the application's `applicants.name`,
   each normalized by the rule below, are exactly equal.

**Name-match rule.** Normalize both sides identically:

1. Apply Unicode NFKC, then case-fold.
2. Replace every character that is not a letter, a digit, or a space with a single space.
   This turns `Mary-Jane` into `mary jane` and `O'Neil` into `o neil`.
3. Collapse runs of whitespace to one space; strip leading and trailing spaces.

Two names match when, and only when, the two normalized strings are equal. Nothing looser
counts: no token containment, no initial expansion, no middle-name tolerance, no suffix
stripping, no nickname table, and token order is significant. A side that normalizes to the
empty string — a `display_name` that is NULL, blank, or punctuation only — never matches,
including against an applicant name that also normalizes to empty.

| `users.display_name` | `applicants.name` | Match | Why |
|---|---|---|---|
| `Dana Whitfield` | `dana whitfield` | yes | equal after case-fold |
| `  Dana   Whitfield ` | `Dana Whitfield` | yes | whitespace collapse and trim |
| `Mary-Jane Cole` | `Mary Jane Cole` | yes | rule 2 turns the hyphen into a space |
| `Dana Whitfield` | `Dana R. Whitfield` | no | the middle initial is an extra token; no middle-name tolerance |
| `D. Whitfield` | `Dana Whitfield` | no | no initial expansion |
| `Dana Whitfield` | `Dana Whitfield Jr.` | no | no suffix stripping |
| `Dana Whitfield` | `Whitfield, Dana` | no | token order is significant |
| `Sean O'Neil` | `Sean ONeil` | no | rule 2 splits only the side carrying the apostrophe (`sean o neil` vs `sean oneil`) |
| `` (blank) | `Dana Whitfield` | no | an empty normalized side never matches |

The last three rows are false negatives by construction: an officer who submits under a
reordered, suffixed, or differently punctuated form of their own `display_name` does not
trigger the check. Exact equality is chosen over a looser rule because the trigger's cost is
paid by whoever it fires on — a second officer's time — and because a rule with tolerances is
one no test can pin down, which is how implementation and tests drift apart.

When both conditions hold, `deny_self_decision` does not raise 403 outright (nothing here proves
wrongdoing — a name match is a heuristic, not identity verification). It instead requires a
second officer, distinct from the caller, to record an explicit approval before the decision
proceeds to the credit pull. An application that fails condition 2 (no plausible name match)
proceeds through the existing single-officer flow unchanged — the ordinary case stays exactly
as fast as it is today.

| Option | Rejected because |
|---|---|
| A. Accept the residual as-is; keep the manual audit query (`docs/runbook.md`) as the only mitigation | PR #38 round 4's own point: this is a live path, not a closed legacy window, and an unscheduled manual query is not a control — it is a query nobody is tasked to run. |
| B. Identity-field matching against stored staff PII (name/DOB/SSN) at decision time, auto-block on match | Requires newly CAPTURING and STORING staff PII the platform does not hold today (`users` carries only `username`/`password_hash`/`role`/`display_name`/`applicant_id`) — a new privacy surface correlating staff identity against applicant identity, on a platform that already has an open PII-in-logs debt (D5). Fuzzy matching on SSN/DOB has real false-positive and false-negative rates (typos, name changes, common values), and an auto-block on a false positive stops a legitimate officer from doing their job with no recourse. An adversarial insider defeats it by supplying a slightly different name at submission — the same weakness Option D accepts, but B compounds it with a new PII store for no better outcome. |
| C. Fail-closed gate: block ANY officer from deciding ANY application with `submitted_by_user_id IS NULL` | `NULL` is the correct, permanent value for the overwhelming majority of genuine applications — the primary channel. This blocks routine decisioning platform-wide, every officer, every anonymous application, to guard against a narrow insider-threat scenario. Disproportionate: trades near-total throughput loss for a control that does not even verify the actual risk (an officer deciding their OWN application) — it fires on every anonymous application, matching or not. |
| **D. Chosen: maker-checker, triggered by a name-match heuristic on the NULL-submitter slice only** | Confines the added friction to applications a cheap, already-available signal (`users.display_name`, no new PII capture) flags as plausibly self-submitted. Every other anonymous application — the common case — is unaffected. |

Option D is a heuristic trigger, not a proof. `users.display_name` is officer-entered free
text, nullable, and not a verified identity field — an officer with no `display_name` set, or
one that genuinely differs from the name used on the application (a married name, a preferred
name), does not trigger the check and reaches the existing exposure unchanged. The maker-checker
step is a tripwire that raises the cost of self-dealing through this channel from zero to
"requires a second officer's cooperation or an unnoticed coincidence," not a guarantee that
closes the gap the way checks 1 and 2 close their cases. The manual audit query in
`docs/runbook.md` stays in place as a backstop, not a replacement.

---

## Consequences

### Positive

- Adds a proactive, reviewable second-officer step for the specific slice of applications a
  cheap signal flags as suspect, instead of leaving the case to an unscheduled manual query.
- The ordinary anonymous-apply channel — the platform's primary intake path — is unaffected for
  every application that does not match a deciding officer's own name.
- Reuses data the platform already collects (`users.display_name`); no new PII capture, no new
  privacy surface.

### Negative / tradeoff (accepted)

- **Heuristic, not verification.** A name-match trigger has both false positives (a borrower who
  happens to share a name with staff, thankfully rare in a real staff roster but not impossible)
  and false negatives (an officer applying under a name that does not normalize to their
  `display_name` — the middle-name, suffix, token-order and apostrophe rows in the rule
  table above). Neither direction is closed by this design.
- **`display_name` is not identity-verified data.** It is operator-entered at account creation
  and can be blank, a nickname, or stale. A blank `display_name` means the check never fires for
  that officer.
- **New operational surface.** A maker-checker step needs a second-officer identity distinct
  from the first, a place for that approval to be recorded, and — depending on the UI decision
  in Sign-off status — a review screen. This is materially more than the account/submitter-id
  checks, which needed no second actor.
- **Does not close the case check 1 and 2 close.** Those checks are exact matches on stored
  identifiers; this is a fuzzy trigger. It narrows the residual, it does not eliminate it.

### Neutral

- No schema change to `applications` or `users` is required for the trigger condition itself
  (`display_name` and `submitted_by_user_id` already exist). Recording the second officer's
  approval does need a new table or column. Its shape is scoped in the implementation plan
  below, which fixes the minimum invariant that shape must satisfy; it is not built.

---

## Cross-cutting concerns

**Security.** Narrows a live segregation-of-duties gap without expanding what PII the platform
stores. The maker-checker approval record itself must not become a second unauthorized-write
surface — it needs the same officer-role gate as the routes it protects.

**Performance.** One normalized string comparison per decision attempt, against a single row
already fetched by `deny_self_decision`. Negligible.

**Scalability.** No change — this does not touch a scan or a batch path.

**Reliability.** Fail-closed default is preserved: if the second-officer approval cannot be
recorded (e.g., a persistence failure), the decision must not proceed, mirroring the existing
`deny_self_decision` fail-closed posture rather than degrading to single-officer on error.

**Maintainability.** Extends `authz.py` and `decision_events`-adjacent code rather than adding a
parallel authorization module, matching the existing seam.

**Cost.** No new infrastructure or dependency. The normalization function is a handful of lines.

**Operational impact.** Introduces a workflow step officers do not have today: a second officer
must be available to approve a flagged decision before it proceeds. Staffing and turnaround
implications are a product question, not an engineering one — flagged in Sign-off status, not
assumed away here.

**Testing impact.** Needs regression coverage for: the trigger firing on each match row of the
name-match rule table, not firing on each non-match row (middle initial, initial-only, suffix,
reversed token order, split apostrophe), not firing when `display_name` is NULL or blank, the
409 status and detail string being what step 2 specifies rather than a 403, the approval gate
blocking until a second, DIFFERENT officer approves, a same-officer "approval" being rejected
(closing the obvious bypass of self-approving one's own flagged decision), and an approval
missing any of the four bindings in implementation step 3 being rejected rather than honored.

---

## Implementation plan (not started — Proposed only)

1. Name-normalization helper implementing the three normalization steps of the name-match rule
   above, shared between the `applicants.name` and `users.display_name` sides of the comparison.
2. `deny_self_decision` gains the trigger condition; on a match it raises HTTP 409 with a plain
   `detail` string ("a second officer must approve this decision before it can run"), not the
   403 the two existing checks raise. 409 is already this service's status for "the
   application's current state does not permit this action": `kyc_gate._block`
   (`services/origination-service/app/kyc_gate.py`) blocks this same decision route with 409
   and `detail="identity verification (KYC) has not passed for this application"`, and the
   boarding and offer routes use 409 the same way (`routers/applications.py`,
   `routers/offers.py`). The service carries no machine-readable error-code field — every
   refusal is a FastAPI `HTTPException` with a human-readable `detail` string — so a caller
   distinguishes this case by status plus that detail text. Introducing a structured error
   code is a separate, service-wide decision and is out of scope here.
3. A minimal approval record — table or reused `decision_events`-style append-only row. The
   storage shape stays open for a follow-up design pass, but the minimum invariant does not. An
   approval must bind all four of:
   - the application it covers (`applications.id`);
   - the original deciding officer it is granted for (`users.id` of the blocked caller);
   - the approving officer who granted it (`users.id`), rejected when equal to the deciding
     officer;
   - the specific flagged decision attempt it authorizes.

   Each binding closes a bypass. Without the deciding officer, any officer's approval unlocks
   any other officer's blocked decision. Without the approving officer, the record cannot show
   a second, different person acted. Without the flagged attempt, one approval becomes a
   standing permission for every later attempt on that application. The attempt binding
   requires the flagged attempt to have an identity the approval can reference — a pending
   decision row, or an attempt id minted when the trigger fires — which the follow-up design
   pass must supply.
4. An endpoint for a second officer to review and approve a flagged application before the
   original caller's decision can proceed.
5. Regression tests per the Testing impact list above, proven per the repository's
   prove-before-fix convention (`make prove` in a detached worktree).

---

## Rollback strategy

The trigger condition is additive to `deny_self_decision` and gates a NEW status, not a
change to the existing two checks' behavior — reverting the commit(s) restores today's
behavior (checks 1 and 2 only) with no data to unwind, assuming the approval record (step 3)
is itself additive and not required by any other path.

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| False negative: officer applies under a name that does not match `display_name` | Accepted residual, stated plainly above; the manual audit query stays as a backstop |
| False positive: a genuine borrower's name coincidentally matches a staff `display_name` | Does not auto-block — routes to a second officer's review, who can approve a genuine coincidence |
| Second officer rubber-stamps without real review | Out of scope for this ADR to solve by software; an operational/training concern, named here so it is not assumed solved by the code |
| Blank `display_name` silently disables the check for that officer | Named directly under Negative/tradeoff; a follow-up could require `display_name` at officer account creation, a separate, smaller decision |
| Approval record becomes a new authorization surface if not gated correctly | Testing impact explicitly requires a same-officer self-approval rejection test |

---

## Assumptions challenged

**"A second, larger control (identity-field matching) is the only way to close this."** The
original `deny_self_decision` docstring (round 1–2) framed identity-field matching (SSN/DOB) as
the only alternative to account comparison, and rejected it as disproportionate. That framing
compared two extremes. A narrower design exists between them: use the identity signal the
platform already holds for staff (`display_name`) only to ROUTE to a second reviewer, not to
auto-decide — smaller than B, more protective than A.

**"Blocking on `submitted_by_user_id IS NULL` is the natural next step after the account and
submitter-id checks."** It is not — those two checks are exact-match closures of specific,
provable cases. `NULL` alone proves nothing; it is the expected value for the common case. A
gate on it alone does not target the risk, it targets the channel.

**"This gap must be closed in the same PR as checks 1 and 2."** Checks 1 and 2 were narrow,
bounded fixes matching the original governance ask exactly. This is a materially larger control
— a new workflow, a new actor (second officer), new state to persist — and deserves its own
scoping and sign-off rather than being built unilaterally inside a review-response loop.

---

## Sign-off status

**Proposed.** Its dependency is satisfied: `fix/self-decision-authz` merged in PR #38, so
`applications.submitted_by_user_id` (migration `0017_applications_submitted_by_user_id.sql`),
`deny_self_decision` and D24 all exist on `main`. Open questions before implementation starts:

1. Is a `display_name`-match heuristic an acceptable trigger, given it is unverified,
   officer-entered data — or does the false-negative rate (blank/mismatched `display_name`)
   make this not worth building at all?
2. Who qualifies as the "second officer" — any other underwriter/admin, or a distinct
   compliance/reviewer role not yet modeled in `_OFFICER_ROLES`?
3. What does the second officer see to make their approval meaningful — full application
   detail, or a purpose-built flagged-review screen? This is a UI scope question, not answered
   here.
4. Is the operational cost (officer availability for a second approval, turnaround delay on
   flagged decisions) acceptable given the likely rarity of genuine self-dealing through this
   channel? This is the client's call, not an engineering one.
