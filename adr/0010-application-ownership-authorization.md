# ADR 0010: Application Ownership Authorization — Bind the Apply Flow to Identity, Deprecate Anonymous Apply

- **Status:** **Proposed** — not yet accepted. Records the design for closing the
  deferred IDOR on the borrower apply flow; needs product sign-off (the anonymous-apply
  deprecation is a product decision) and compliance review before build.
- **Date:** 2026-07-16
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

### 1. Ownership column

Add `applications.owner_user_id TEXT` (nullable; see migration). On `POST /applications`
(submit), stamp it with the caller's `X-User-Id`. Submit therefore requires an
authenticated borrower session.

### 2. Officer-OR-owner check on the sensitive routes

`GET /applications/{id}`, `POST /applications/{id}/decision`,
`POST /applications/{id}/accept`, and `POST /offer` authorize as:

- **officer** — `X-User-Role ∈ {underwriter, admin}` may act on any application (their
  job; reuses the round-4 `_require_officer` predicate), **OR**
- **owner** — `owner_user_id == X-User-Id`, **else 403**.

Anonymous callers (no session → no `X-User-Id`) are rejected. This single check covers
both the PII-read enumeration and the write triggers.

### 3. Anonymous apply is removed

A borrower must authenticate to apply. "Apply without an account" (CLAUDE.md) is
explicitly deprecated by this ADR; a guest, if retained, may browse marketing/eligibility
but cannot submit, decision, or accept.

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

- `owner_user_id` is **nullable**. Existing rows created anonymously get `NULL`.
- NULL-owner rows are **officer-only** (no borrower can match a NULL owner), grandfathering
  legacy data without exposing it to arbitrary borrowers. New rows always bind an owner.
- Serial ids can remain (ownership, not obscurity, is the control); optionally move to
  non-sequential ids later as defense-in-depth, out of scope here.

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
| Product | **Pending** — the anonymous-apply deprecation (§3) is a product decision |
| Engineering | **Proposed** — this ADR (ownership binding + officer-OR-owner check) |
| Compliance / Legal | **Pending** — confirm the retention/authorization treatment of NULL-owner legacy rows |
| Data owner | **Pending** — `owner_user_id` column + migration on the shared DB (ADR 0002) |
