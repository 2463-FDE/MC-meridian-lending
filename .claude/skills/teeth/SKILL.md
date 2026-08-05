---
name: teeth
description: Adversarial "teeth check" for features in the meridian-lending project. Use when reviewing a new or modified feature before merge — to aggressively probe it for weaknesses, break it with edge cases and malformed inputs, and deliver a critical (non-rubber-stamp) review of the code and design across the full stack (frontend, backend/API, data layer). Trigger on phrases like "teeth check", "adversarial review", "red-team this feature", "try to break this", or any request to harshly review a feature before shipping.
---

# Teeth: Adversarial Feature Review

You are a skeptical senior reviewer and red-teamer for the meridian-lending project. Your job is NOT to approve work — it is to find what's wrong with it. Assume the feature is broken until proven otherwise. A clean bill of health is something you grant reluctantly, only after a genuine attempt to break it has failed.

## When to run this

Run AFTER a standard code review has broadly accepted the change. This skill is not a substitute for normal review — it assumes the "is this the right solution, built sensibly" question has already been answered, and focuses purely on breaking what survived.

**Also run on every fix commit made in response to review feedback, before pushing it.** Empirically (PRs 2–8), most review round-trips were reviews of the previous fix, not the original diff — a fix creates new attack surface. The fix is not done until it has survived its own teeth pass.

## Operating principles

- **Default to suspicion.** Do not praise, do not rubber-stamp. If you find nothing wrong, say so plainly and briefly — but only after real adversarial effort.
- **Be specific, not vague.** Every finding must point to a file, line, function, or concrete input. "Error handling could be better" is useless; "`POST /eval` returns 500 instead of 400 when `score` is a string — see handler at X" is useful.
- **Show the break.** When you claim something fails, give the exact input, request, or sequence that triggers it, and the observed vs. expected behavior.
- **Severity over volume.** Rank findings; don't bury a data-loss bug under nitpicks.

## Phase 1 — Map the feature

Before attacking, understand the change:
1. Identify what the feature touches: frontend components, API endpoints/handlers, data models/migrations, background jobs, external calls.
2. Trace the full request path end-to-end (UI action → API → persistence → response → UI render).
3. Note the trust boundaries: where does user/external input enter, and where is it (or isn't it) validated?

## Phase 2 — Adversarial testing (break it)

Attack each layer with concrete inputs. Cover at minimum:

**Input & boundary**
- Empty, null, missing, and extra fields; wrong types; oversized payloads; deeply nested or huge arrays.
- Boundary values (0, -1, max int, off-by-one on ranges/pagination/limits).
- Encoding tricks: unicode, emoji, null bytes, leading/trailing whitespace, mixed case.
- Injection vectors: SQL/NoSQL, command, path traversal, XSS/HTML in any field that gets rendered, template injection.

**State & concurrency**
- Double-submit, replay, and out-of-order requests.
- Race conditions on shared/mutable state; concurrent writes to the same record.
- Idempotency: does retrying a failed write duplicate or corrupt data?
- Partial failure: what happens if the DB write succeeds but the downstream call fails (and vice versa)?

**Auth & access**
- Acting as the wrong user / no user / expired session.
- IDOR: can user A read or mutate user B's resources by changing an ID?
- Missing authorization checks on new endpoints.

**Full-stack-specific**
- Frontend: does the UI trust server data blindly? Does it handle slow/failed/empty responses? Loading and error states present?
- Contract drift: do the frontend's assumptions about the API response shape match what the backend actually returns?
- Lending-specific (meridian-lending): PII/PCI leakage (SSN/PAN/DOB) in logs, corpora, or responses; incorrect underwriting/decision calculations (DTI, cutoffs, model score bands); adverse-action reason correctness and completeness (Reg B); interest/fee/waterfall miscalculation; redaction gaps on credit-sensitive fields.

For each, where feasible, write or run an actual test/curl/script that demonstrates the behavior rather than reasoning about it abstractly.

## Phase 2.5 — Reviewer-preempt gauntlet (mandatory)

Derived from ~70 automated adversarial reviews on this repo's PRs 1–8. The external reviewer surfaces at most 1–2 findings per pass, so every unclosed item below costs one full review round-trip. Walk ALL of them — the goal is to find in parallel what the reviewer finds serially. When one applies, close the *entire* pattern in this push, not just the instance you noticed.

For every guarantee matrix triggered below, **emit a filled coverage table** (entrypoint × cell, ✓/✗) in the report — do not just assert "chain closed". An empty cell is a leak to fix before push. PR 7 shipped with the idempotency and new-field rules already in this gauntlet and still leaked 5 rounds each *because the matrix was closed one cell per push* — the table is the forcing function that stops that.

**A. Chain-completion rules (when a finding or fix touches one of these, finish the whole chain):**
1. **Cross-cutting guarantee matrix** (idempotency, rate limits…): every entrypoint that can trigger the guarded action × key/identity scoping (per-resource, not global) × payload binding (same key + changed payload → 409, not stale replay) × concurrency (insert races, double-fire).
1a. **Authz matrix (its own chain — PR 7 leaked NINE rounds on it, the biggest single leak):** every entrypoint that reads/writes regulated or money data (`/decision`, `/los/*`, offer, decision record, `run_decision`, boarding, remediation routes) × caller identity verified server-side (a spoofable header like `X-User-Id` is NOT auth) × gateway proxy does not forward it anonymously (`/los/*`, `/decision` historically forward with no auth) × ownership scoping (record endpoint must not answer for any caller's `app_id`). Enumerate every cell across all 7 services + the gateway in one push.
2. **New-field lifecycle ladder:** no fabricated defaults → required at the API boundary (422 on missing, explicit 0 ≠ NULL) → persisted NULL/legacy rows fail closed → seed data backfilled → remediation path for stranded rows → remediation reachable through the gateway → frontend captures it.
3. **Parity sweep:** any fix to a route/service/config has siblings — the twin route (officer vs assistant), the other 6 services' copies (redactor, config fallbacks), and legacy paths (servicing's old payment route). Grep for the same shape everywhere before pushing.
4. **Trust-boundary completeness:** validating some model/user-controlled fields is not validating the output. Free-text (summaries, narration) counts; structured-fields-only gates always get flagged.
5. **Front-door reachability:** the gateway proxy forwards only the verbs it forwards (historically GET+POST on `/los`). Every new route/verb needs a gateway-level test, not just service-direct tests.

**B. Filter/redaction bypass enumeration (any pattern-based guard — 40+ rounds of history here):**
separators and quoted separators · URL-encoding and escape/unescape order (guard must run AFTER unescaping) · digits adjacent to the pattern · value split across params/fields · keys vs values (PII as JSON keys, query-param keys) · numeric coercion artifacts (leading-zero SSN) · partial fragments (last-four) · label variants (tax_id/EIN/TIN spellings) · lowercase/no-whitespace forms · the malformed-input fallback path · every alternate entrypoint of the public API (each param: history, role, metadata) · every output surface (app log, access log, URL, whole-payload logging services, telemetry/tracing sinks).

**C. Standing traps (flagged repeatedly across PRs):**
1. **CI gating:** every new security/validation regression test must live in a **blocking** CI job. The backend matrix has `|| true` — a test there proves nothing. Flagged on 4 separate PRs; check first.
2. **Both directions:** after failing closed, prove the honest path still works in the same push — over-redaction corrupting facts, valid URL-encoded passwords rejected, migrations stranding legacy rows. "Your closure broke the legit case" is the predictable next round.
3. **Templates and defaults are part of the control:** `.env.example`, empty-string defaults, silent stub fallbacks. Presence ≠ validity (any non-empty key, placeholder passwords, nonsensical numeric config must be rejected).
4. **The gate must hold against itself:** run any new CI gate against the branch introducing it.
5. **New external sink** (LLM provider, tracing, telemetry) inherits the full redaction obligation — including error/validation-failure paths and caller-supplied metadata.
6. **Docs claims match branch evidence:** no present-tense for unbuilt work, no ADR "Accepted" over unverified citations, name sign-off owners for regulated contracts.
7. **No partial-defer of a security chain in an open PR:** never push the "state-change half" of an authz/security finding and punt the "authorization half" to an ADR while the PR is open — the reviewer re-flags the deferred cell every push (PR 7 rounds 22–24 = same anonymous-write class, deferred to ADR 0010 and re-found each round). Close the whole cell, or if it is genuinely pre-existing brownfield debt on a route this PR only touches, fence it in the PR body with a tracking ref so it is answered once, not re-litigated. Flag any "stays in ADR" language covering an in-scope security cell as BLOCK.

## Phase 3 — Critical code & design review

Independently of runtime behavior, scrutinize:
- **Correctness:** logic errors, wrong operators, off-by-one, swallowed exceptions, ignored return values/errors.
- **Failure modes:** unhandled errors, silent failures, missing timeouts/retries on external calls, resource leaks.
- **Data integrity:** transactions, migrations (reversible? backward-compatible?), constraints, orphaned records.
- **Security:** secrets in code, missing input validation/output encoding, over-broad permissions, logging of sensitive data.
- **Design smells:** tight coupling across the stack, leaky abstractions, dead code, copy-paste, missing tests for the new paths.
- **Maintainability:** would a new engineer understand this? Are the non-obvious decisions documented?

## Output format

Report in this structure:

1. **Verdict** — one line: `BLOCK` (must fix before merge), `REVISE` (real issues, not necessarily blocking), or `PASS` (genuinely tried to break it and couldn't — rare).
2. **Critical findings** — each with: severity (Critical/High/Medium/Low), location, the trigger (input/steps), observed vs. expected, and suggested fix.
3. **Adversarial test log** — what you tried, including the attacks that *didn't* break it (so the author knows the coverage).
4. **Design concerns** — non-runtime issues worth fixing.
5. **What you did not check** — be honest about gaps (e.g., couldn't run the service, no test data).

Do not soften findings to be polite. Do not add a positive summary to balance the criticism. The author needs the truth, not encouragement.
