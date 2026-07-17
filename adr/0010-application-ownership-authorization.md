# ADR 0010: Application Ownership Authorization — Bind the Apply Flow to Identity, Deprecate Anonymous Apply

- **Status:** **Accepted (Phase A) / Proposed (Phase B).**
  - **Phase A — officer-OR-owner enforcement — BUILT and accepted.** The four
    orchestration routes (plus the application list and the offer read) now authorize as
    officer-OR-owner in origination, closing the anonymous IDOR. This needed **no schema
    change and no product decision**: ownership is derived from data that already exists
    (see Decision §1), so it shipped in the Week-3 review.
  - **Phase B — deprecate anonymous apply + borrower self-registration — still proposed.**
    Requiring a login to *apply* (so every new application binds to an owner) and the
    signup/frontend work that implies is a product decision (the anonymous-apply
    deprecation) and remains pending product + compliance sign-off. Phase A does **not**
    depend on it.
- **Date:** 2026-07-16 (Phase A accepted 2026-07-17)
- **Author:** Claude Code
- **Related:** ADR 0002 (single shared DB), ADR 0004 (service decomposition), ADR 0009
  (decisioning assistant), the round 2–4 internal-service auth work
  (`X-Internal-Service` gates + gateway inbound trust-header stripping),
  CLAUDE.md ("gateway does NOT enforce role authz … kept on purpose")

---

## Context

The gateway proxies `/los/*` **anonymously** so a borrower can apply without an account
(`services/gateway/app/main.py`, documented as intentional in CLAUDE.md). The Week-3
review closed the *input-fabrication* half of the resulting exposure: `POST /decisions`,
`/kyc/check`, `/offers`, and `/board` now require the internal-service secret, so an
external caller can no longer inject fabricated underwriting inputs. What remains open is
**authorization on the borrower-facing orchestration routes**, which are reached by the
browser with no session:

- `GET /los/applications/{id}` returns applicant PII (name, email, phone, address),
  decision outcome, and offer. Application ids are **serial integers**, so an anonymous
  caller can enumerate `1,2,3,…` and harvest every applicant's PII and credit decision.
  This is the sharpest exposure — a confidentiality leak, not just an integrity one.
- `POST /los/applications/{id}/decision` triggers a decision on any app id: it performs a
  **credit-bureau pull** on a real person, appends a regulated `decision_events` row, and
  moves the mutable `decisions` pointer. Absent an idempotency key it re-decides, so it is
  repeatable.
- `POST /los/applications/{id}/accept` **boards the loan** (creates `loans` + `balances`,
  sets `status='funded'`) — unauthorized loan origination on someone else's application.
- `POST /los/offer` generates and **persists** a TILA disclosure offer (`offers` row,
  later read by `/accept` to board) for a caller-supplied app id. A round-9 review fix
  bound its money inputs to the stored application (they are no longer caller-supplied),
  but the anonymous *trigger* for any app id remains — the same confused-deputy write as
  `/decision`, so it is covered by the same officer-OR-owner check below.

These routes cannot be closed with the internal-service secret used elsewhere: the
**borrower** legitimately calls them from the browser (the apply page triggers
`/decision` and `/accept` with no login), so the gateway strips any client-supplied
`X-Internal-Service` and the frontend cannot hold it. The officer-role gate used for the
assistant (ADR 0009 §5 / round 4) also cannot apply here, because that would break
borrower self-service.

The missing primitive is **per-application ownership**: there is no identity bound to an
application, so "may this caller act on this app id?" cannot be answered.

Two facts make this tractable now rather than a greenfield build:

1. A **`borrower` role and login already exist** (demo login `maria`); the gateway
   resolves the session and forwards `X-User-Id` / `X-User-Role` downstream.
2. Round 2 made those headers **trustworthy** — the gateway strips any client-supplied
   copy on every proxy path, so a downstream service may treat `X-User-Id` as authentic.

## Decision

**Bind each application to the authenticated borrower's identity at creation, and
authorize the orchestration routes as officer-OR-owner. Deprecate anonymous apply.**

### 1. Ownership is derived from existing data — no new column (Phase A, built)

The design originally proposed a new `applications.owner_user_id` column. Implementation
found the ownership relation **already exists** and needs no schema change: a borrower
login carries `users.applicant_id`, and an application carries
`applications.applicant_id`, so the user who owns an application is the one whose
`users.applicant_id` equals it. The check resolves the caller's `applicant_id` from
`users` by the forwarded `X-User-Id`. (Seeded demo borrower `maria` → `applicant_id = 1`;
officers carry `applicant_id = NULL`.) No `owner_user_id` column, no migration.

### 2. Officer-OR-owner check on the sensitive routes (Phase A, built)

`GET /applications/{id}`, `POST /applications/{id}/decision`,
`POST /applications/{id}/accept`, `POST /offer`, and `GET /applications/{id}/offer`
authorize as (`services/origination-service/app/authz.py::require_officer_or_owner`):

- **officer** — `X-User-Role ∈ {underwriter, admin}` may act on any application (their
  job), **OR**
- **owner** — the caller's `users.applicant_id` equals the application's `applicant_id`.

The application *list* (`GET /applications`) is officer-only (it dumps applicant PII
across the whole book). Anonymous callers (no session → no `X-User-Id`) are rejected. A
non-officer, non-owner is denied as **404, not 403** — no existence oracle, so serial-id
enumeration cannot even confirm which application ids are real.

### 3. Anonymous apply is deprecated — Phase B, NOT built

Requiring a login to *apply* (so every new application binds to an owner via the caller's
`applicant_id`) is deferred. `POST /applications` remains anonymous for now. Consequence
of shipping Phase A without Phase B: an application created anonymously has an
`applicant_id` that no user login owns, so **only an officer** can decision/offer/accept
it — a logged-in borrower (e.g. `maria`) self-serves their own application, but a brand-new
anonymous applicant cannot self-serve past submit until an officer acts or Phase B lands.
That is the intended "deny anonymous callers" behavior, not a regression. Phase B (login
before apply + borrower self-registration + the frontend changes) is a product decision
and its own PR.

### 4. Reuse, don't reinvent

No new auth artifact (capability token, per-app secret) is introduced. The check rides
the existing session → `X-User-Id`/`X-User-Role` path that round 2 already hardened, so
officers and borrowers share one authorization model.

## Consequences

### Positive

- Closes the IDOR on both reads (PII enumeration) and writes (unauthorized credit pull /
  loan boarding) with one ownership check.
- Future-proof: this *is* the applicant-identity model, not interim scaffolding.
- Reuses the trustworthy `X-User-Id` from the round-2 gateway strip; no secret to mint,
  thread through the frontend, and later remove.

### Negative

- **Removes anonymous apply** — a product-visible behavior change (contradicts the current
  CLAUDE.md posture). Requires product sign-off; may need a borrower self-registration
  flow if one does not exist.
- Touches schema (`owner_user_id` + migration), three routes, and the frontend (borrower
  login before apply; carry the session).
- Legacy/anonymous rows have `owner_user_id = NULL` and need a policy (below).

### Forward compatibility (future RBAC)

This ADR is deliberately the first concrete authorization *rule* in the platform, not a
one-off. It is shaped to be the seed of a future role-based access-control layer:

- The enforcement inputs — session-resolved `X-User-Role` and `X-User-Id`, trustworthy
  after the round-2 gateway strip — are exactly what an RBAC layer keys on. No new plumbing
  is needed to generalize.
- `{officer, owner, anonymous}` here is the minimal instance of `{role grant, resource
  ownership, deny}`. A later RBAC layer replaces the inline `officer-OR-owner` predicate
  (§2) with a policy lookup — role → permission on a resource type, plus the same
  ownership predicate — without changing the routes' contract or the `owner_user_id`
  column.
- Existing roles (admin, underwriter, csr, borrower) become the initial role set; per-route
  checks like `_require_officer` become `require_permission("application:decision")`, with
  `owner_user_id` supplying the row-level scope RBAC alone does not.

So the recommendation is to build §1–§4 now as the authorization primitive, and treat a
central RBAC policy module as the natural next ADR that generalizes it — this work is a
stepping stone toward it, not throwaway.

### Migration

- **No migration for Phase A** — ownership is derived from the existing
  `users.applicant_id` ↔ `applications.applicant_id` link, so there is no new column to
  add and no legacy backfill.
- The "NULL-owner legacy row" concern reduces to a **behavior note**, not a data task:
  an application whose `applicant_id` is owned by no user login (anonymously created, or a
  seeded/legacy row) is **officer-only** — no borrower's `applicant_id` can match it. This
  grandfathers legacy data without exposing it to arbitrary borrowers.
- Serial ids can remain (ownership, not obscurity, is the control; the 404-not-403 denial
  removes the enumeration oracle anyway); optionally move to non-sequential ids later as
  defense-in-depth, out of scope here.

## Alternatives considered

1. **Per-application capability token** — mint an unguessable token at submit, return it,
   require it on `/decision`/`/accept`/`GET`. Preserves truly anonymous apply. Rejected
   *if* applicant accounts are the direction: it is throwaway scaffolding duplicating an
   identity mechanism that already exists. It remains the right choice only if anonymous,
   no-login apply must be kept.
2. **Full borrower account system** — the ownership model here is the minimal slice of
   this; a broader accounts feature (profiles, saved applications) is a superset, deferred.
3. **State-machine / one-shot guards** (decision once, accept only after approved offer) —
   defense-in-depth that limits abuse but does not answer "whose application is this,"
   so it does not close the cross-app trigger or the read. Complementary, not a
   replacement.
4. **Enumeration hardening** (UUID ids, rate limits) — raises attacker cost but is not
   authorization; only worthwhile alongside the ownership check.

## Sign-off status

| Owner (role) | Status |
|--------------|--------|
| Engineering | **Accepted (Phase A)** — officer-OR-owner check on the orchestration/read routes, derived from `users.applicant_id`; shipped in the Week-3 review with a blocking authz test gate |
| Product | **Pending (Phase B only)** — the anonymous-apply deprecation (§3) + borrower self-registration is a product decision; Phase A does not need it |
| Compliance / Legal | **Pending (Phase B only)** — confirm the treatment of anonymously-created (owner-less) applications now that they are officer-only |
| Data owner | **N/A for Phase A** — no schema change; ownership is derived from existing columns |
