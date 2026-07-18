# ADR 0010: Application Ownership Authorization — Bind the Apply Flow to Identity, Deprecate Anonymous Apply

- **Status:** **Accepted (Phase A + Phase B, both BUILT).**
  - **Phase A — officer-OR-owner enforcement — BUILT.** The four orchestration routes
    (plus the application list and the offer read) authorize as officer-OR-owner in
    origination, closing the anonymous IDOR. No schema change, no product decision:
    ownership derives from data that already exists (see Decision §1).
  - **Phase B — anonymous applicant self-service — BUILT via the continuation-token
    variant (Alternative 1), NOT via deprecating anonymous apply.** A review round showed
    Phase A alone broke the public apply flow (a logged-out applicant could submit but then
    got 404 on their own decision/offer/accept). Rather than force a login (deprecate
    anonymous apply + build borrower signup — a product decision with no signup flow in
    existence), `POST /applications` now issues an **unguessable per-application
    continuation token**, and the application-scoped routes authorize on officer OR owner
    OR a valid token for that application. This preserves anonymous apply, closes the
    serial-id IDOR (the token, not the guessable id, is the authorization), and needs no
    product/compliance sign-off. See Decision §3 and Alternative 1 (now the chosen path).
- **Date:** 2026-07-16 (Phase A + Phase B accepted 2026-07-17)
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

### 3. Anonymous apply is PRESERVED via a scoped continuation token (Phase B, built)

Anonymous apply is kept, not deprecated. `POST /applications`
(`routers/applications.py`) issues an unguessable per-application continuation token
(`secrets.token_urlsafe(32)`), returns it once in the submit response, and persists it to
`applications.continuation_token`. `authz.require_officer_or_owner` accepts it as a third
authorization path: officer OR owner OR a valid continuation token for THAT application.
The token is a capability scoped to one application id (a token minted for app A cannot
authorize app B), so the logged-out applicant completes their own decision/offer/accept
while serial-id enumeration stays closed. Origination authorizes on `X-Application-Token`;
that header is now set **only by the gateway** (it strips any client-supplied copy, as it
does `X-User-*`) — see the browser-side custody note below. A NULL token (officer-created/
legacy row) has no token path, so those stay officer-OR-owner only. No login, no signup.

**The continuation token also carries *self-service authenticated* applicants, not just
anonymous ones (PR #7 review).** `POST /applications` creates a fresh applicant it never links
to `users.applicant_id`, so owner authz (`users.applicant_id == applications.applicant_id`)
can never match a submission the caller made themselves — a logged-in borrower on the apply
page is, for that application, in the same position as an anonymous one. The gateway therefore
issues the resume session/cookie for **every** submit, authenticated or not; owner authz stays
meaningful for applications an operator later associates with a user (Phase C), and for the
seeded borrower/application links. (Rejected alternative: binding the new application to
`users.applicant_id` at submit — it would break the one-applicant-per-application invariant
intake and the abandon compensation rely on, and reusing the applicant drags in its existing
`kyc_checks`, entangling the ADR 0011 KYC gate. Deferred to the Phase C `owner_user_id` work.)

**Browser-side custody: server-side session, never localStorage (PR #7 review).** The raw
token is a bearer credential for money-moving routes, so it is never handed to the browser
(localStorage/JS would expose it to any same-origin script or XSS). Instead the **gateway**
holds it: on submit it stashes `{app_id, token}` in Redis under an opaque session id
(`auth.create_resume_session`), returns the browser only an **HttpOnly, Secure, SameSite=
Strict** cookie (`meridian_resume`, `Path=/los`) carrying that id, and **strips the raw
token from the submit response body**. On each application-scoped `/los` request the gateway
resolves the cookie and re-injects `X-Application-Token` downstream — scoped to the app id
in the path, or (for `/los/offer`, whose app id is in the body) validated per-app by
origination. On accept it revokes the Redis session and clears the cookie. The frontend
persists only the non-sensitive `app_id` for resume; the cookie (sent with
`credentials:"include"`) is the actual capability. Result: the token never exists in
browser-readable storage, is server-side revocable, and a stolen cookie yields only an
opaque, expiring, HttpOnly session id — not the credential.

**Token hardening at rest (PR #7 review).** The persisted token (in Postgres, distinct from
the Redis resume session above) is also not stored or lived unbounded:

- **Hash at rest, versioned dedicated pepper.** `applications.continuation_token` stores a
  version-tagged keyed digest `"<version>:<HMAC-SHA256>"` (`authz.hash_token`), never the raw
  token; the raw value exists only in the applicant's possession after the one-time submit
  response. A DB read / backup / logged row yields a non-replayable digest. The HMAC key is a
  **dedicated pepper** (`CONTINUATION_TOKEN_KEYS`), separate from `INTERNAL_SERVICE_TOKEN`, so
  rotating the service-auth secret does not invalidate live resume tokens. Keys are versioned:
  `authz.verify_token` picks the key matching the stored version, so a pepper rotation keeps
  pre-rotation tokens verifiable until they expire (keep the old key configured ≥
  `CONTINUATION_TOKEN_TTL_DAYS`, then drop it). `CONTINUATION_TOKEN_KEYS` is **required outside
  development** (PR #7 review): unset, `missing_required_secrets` reports it so `/health` is
  unhealthy and `authz.hash_token` **refuses to issue** a new token rather than silently
  hashing it with `INTERNAL_SERVICE_TOKEN`. The service-token fallback survives on the
  **verify** path only — pre-existing rows hashed under the old coupling (a `legacy`-versioned
  digest) still verify until they expire, so decoupling never strands live sessions. In
  development the fallback also applies to hashing, for local-demo convenience only.
- **Expiry.** `continuation_token_expires_at` (migration 0009) time-boxes the token
  (`CONTINUATION_TOKEN_TTL_DAYS`, default 7); authz rejects a token past — or with a NULL —
  expiry, so it fails closed.
- **Single-use at funding.** `accept_offer` clears the token hash + expiry to NULL in the
  same statement that sets `status='funded'`, so the terminal money action retires the
  bearer capability — token residue cannot re-drive a funded application.

**Not done (deliberate; product decision).** No separate short-lived *acceptance* token and
no capability split between resume/read and `/accept`: a "stronger authenticated applicant
action before boarding" requires an authenticated applicant, which the anonymous flow does
not have (no login, no verified email/SMS channel — the same wall as the pre-migration
recovery in §Migration). Splitting the capability is only meaningful once anonymous apply
is deprecated in favor of applicant accounts (the retirement path noted below); until that
product decision, the token is hardened but remains a single capability.

Why this over deprecating anonymous apply: forcing a login would need a borrower
self-registration flow that does not exist and a product decision to drop "apply without
an account" (CLAUDE.md). The token closes the IDOR without either. If applicant accounts
later become the direction, the owner path (§2) already exists and the token path can be
retired.

### 4. Reuse where it fits; one small new artifact where it does not

The officer/owner check rides the existing session → `X-User-Id`/`X-User-Role` path that
round 2 hardened. The one new artifact is the continuation token (§3) — justified because
the borrower has no session to key on during anonymous apply, which is exactly the case
the existing identity path cannot cover.

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
- **New Redis coupling on the self-service apply flow (PR #7 review).** Moving token custody
  to a server-side resume session (Phase B §3) makes every self-service submit — anonymous OR
  authenticated (see §3: neither is owner-linked) — and every subsequent
  resume/decision/offer/accept depend on Redis to create/resolve the session. Redis was
  already required for login sessions, but self-service submit previously had no such
  dependency (the token rode in the response body). Submit is made atomic from the
  applicant's perspective: the gateway (1) refuses a submit up front with a
  retryable **503** when Redis is unreachable, so origination never commits an application
  whose one-time token could not be stored; and (2) rides a transient blip in the tiny
  post-check window with a bounded retry on the session write, returning a controlled 503
  (never a 500 that discards the raw token) if it still fails. Resume/decision/offer/accept
  fail closed on a Redis outage (no capability resolved → denied), never a silent bypass.
  The gateway `depends_on: redis` (compose) and the `/health` readiness probe surface
  reachability. If Redis dies within that post-check window and stays down through the
  retry, the gateway issues a **compensating rollback** — an internal-only
  `POST /applications/{id}/abandon` that deletes the just-committed application (guarded to
  INERT rows only: no decision/offer/loan is ever deletable) — so the applicant's retry
  creates one clean application, not a duplicate plus a stranded PII-bearing orphan. The
  delete is **transactional and cascades PII** (PR #7 review): submit runs KYC before the
  session write, so the inert application usually already has a `kyc_checks` row keyed to its
  `applicant_id`; that FK has no `ON DELETE CASCADE`, so `abandon` deletes the application, its
  `kyc_checks` rows, and its applicant in one transaction (children before parent) — otherwise
  the applicant delete would FK-fail after the application delete committed under psycopg2
  per-statement autocommit, re-stranding the very PII the rollback exists to remove.
  **Residual:** the compensation is best-effort; if origination is *also* unreachable at
  that moment (or `INTERNAL_SERVICE_TOKEN` is unset), the inert application is left for
  officer reconciliation (logged). A fully transactional submit would need submit
  idempotency, but the one-time raw token cannot be re-issued on an idempotent replay
  (origination stores only its hash), so refuse-before-commit + retry + compensating-delete
  is the correct closure here.
- **No single end-to-end test spans gateway ↔ Redis ↔ origination.** Each seam is unit-
  tested — the gateway trust-boundary/resume-cookie behavior in `test_proxy_methods.py`
  (blocking `gateway-trust-boundary-gate`, Redis + origination mocked), and origination's
  per-application token authorization in `test_authz.py` (blocking `adr-0010-authz-gate`).
  The full chain against live Redis + origination is covered by the compose smoke test, not
  a unit gate.

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

- **Phase A: no migration** — ownership derives from the existing `users.applicant_id` ↔
  `applications.applicant_id` link, so there is no new column and no backfill.
- **Phase B: migration 0008** adds the nullable `applications.continuation_token` column
  (`ADD COLUMN IF NOT EXISTS`, non-destructive; the origination readiness probe reports
  `schema_not_ready:applications.continuation_token` on a volume that predates it, so an
  unmigrated deploy fails `/health` loud instead of 500-ing submit). Legacy rows keep a
  NULL token and stay officer-OR-owner only.
- The "NULL-owner legacy row" concern reduces to a **behavior note**, not a data task:
  an application whose `applicant_id` is owned by no user login (anonymously created, or a
  seeded/legacy row) is **officer-only** — no borrower's `applicant_id` can match it. This
  grandfathers legacy data without exposing it to arbitrary borrowers.
- **Pre-migration in-flight anonymous applications (compatibility).** Before this feature
  the application-scoped routes were unauthenticated, so an anonymous applicant completed
  the flow with no credential; after this deploy a token is required and a pre-migration
  anonymous row has none, so its logged-out applicant cannot self-serve. Migration 0008
  deliberately does **not** backfill tokens — an undelivered token (no verified email/SMS
  channel exists) is false safety. Recovery is **officer-mediated**: officers act on any
  application and the officer underwriting UI now exposes the full flow (re-run identity
  check, decision, offer, accept), so an officer can advance a stranded application on the
  applicant's behalf — the same manual-operator recovery rung as migration 0007. A fresh
  `db/init` volume + the seed have no such rows (seed applications carry an `applicant_id`).
  A self-serve verified-channel resume flow needs a delivery channel + product sign-off and
  is out of scope here.
- Serial ids can remain (ownership, not obscurity, is the control; the 404-not-403 denial
  removes the enumeration oracle anyway); optionally move to non-sequential ids later as
  defense-in-depth, out of scope here.

### Rollout / cutover plan (PR #7 review)

Enabling the gate is a user-visible compatibility change for any **pre-migration in-flight
anonymous application** (created before Phase B, so `continuation_token IS NULL`, and owned
by no user login), which loses its self-serve path. This is a planned cutover step, not a
silent break:

1. **Enumerate affected rows before cutover.** On the target volume, run:

   ```sql
   SELECT a.id, a.status, a.created_at
   FROM applications a
   LEFT JOIN users u ON u.applicant_id = a.applicant_id
   WHERE a.continuation_token IS NULL          -- predates Phase B token issuance
     AND u.id IS NULL                          -- not linked to any borrower login
     AND a.status NOT IN ('funded', 'withdrawn', 'declined');  -- still in flight
   ```

2. **If the query returns rows, drain/handle them before enabling the gate** — an officer
   completes or advances each on the applicant's behalf (officer-mediated recovery above),
   or product issues approved comms and links the applicant to a borrower account. Only then
   flip the gate on.
3. **If it returns zero rows, cutover is clean.** A fresh `db/init` volume + the seed have
   none (seed applications carry an `applicant_id` and are officer/owner-managed), so in this
   repo's environment the query is empty and no drain step is required.

**Why not conditionally exempt pre-migration rows from the gate** (the review's "do not
enforce until…" phrasing): exempting exactly the old, low, guessable serial ids would
reopen the anonymous serial-id IDOR this ADR closes — for the precise rows an attacker can
enumerate. The compatibility path is therefore drain-then-cutover + officer-mediated
recovery, never a standing carve-out that leaves regulated routes anonymously reachable.

## Alternatives considered

1. **Per-application capability token** — mint an unguessable token at submit, return it,
   require it on `/decision`/`/accept`/`GET`. Preserves truly anonymous apply. **CHOSEN for
   Phase B** (see Decision §3): anonymous, no-login apply is a product feature with no
   signup flow to replace it, so the token — a capability scoped to one application — is
   the fitting closure, not throwaway. The officer/owner paths (§1–§2) still stand for
   authenticated callers; the token only covers the logged-out applicant.
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
| Engineering | **Accepted (Phase A + B)** — officer-OR-owner check + anonymous continuation token; shipped in the Week-3 review with a blocking authz test gate + a live-stack smoke |
| Product | **Not required** — anonymous apply is preserved (§3), so the deprecation decision that was pending is moot; the token needs no product sign-off |
| Compliance / Legal | **Informational** — anonymously-created applications remain reachable only by an officer, the owning borrower, or the token-holder; no owner-less PII exposure |
| Data owner | **Accepted** — migration 0008 adds the nullable `continuation_token` column (non-destructive, readiness-gated) |
