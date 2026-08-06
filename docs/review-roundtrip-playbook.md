# Review Round-Trip Playbook

How to converge with the automated adversarial reviewer (`@codex-review` / JesterCharles bot)
in 2–3 rounds instead of 10–26.

Derived from ~70 real bot reviews across PRs 1–8 (PR 2: 16 rounds, PR 3: 26, PR 4: 17,
PR 6: 21, PR 7: 11, PR 8: 5).

## Why round-trips explode

1. **The bot hard-caps output at 1–2 findings per pass** — always one "No-ship" verdict.
   Proof: the `monthly_debt: 0` fabrication existed in PR 7's round-1 diff but wasn't
   surfaced until round 7. Findings queue behind the cap, so the bot will never hand over
   the full set in one pass. The full batch has to be produced locally, before pushing.
2. **Most rounds review the previous fix, not the original diff.** A fix creates new
   review surface (new endpoint, new constraint, new failure mode). A fix is not done
   until it has survived its own adversarial pass.
3. **Re-triggering without pushing is a pure no-op.** 9 wasted "no new commits" replies
   on PR 6, 4 on PR 7.

## The loop, in order

### Before the first push (feature)

1. Build the feature; normal review passes. Ship-thin the scope first — a 41-commit,
   3-service PR (PR 7) drags brownfield debt into review surface and multiplies rounds.
2. Run the **teeth** skill on the diff — including its Phase 2.5 reviewer-preempt
   gauntlet (chain-completion rules, filter-bypass enumeration, standing traps below).
   teeth must **output a filled matrix table** (each guarantee's entrypoint × cell, ✓/✗)
   — an empty cell is a leak you fix now, not one the bot finds serially later. A prose
   reminder to "close the chain" is not enough: PR 7's playbook already had the
   idempotency and new-field rules and they still leaked 5 rounds each.
3. Fix everything teeth finds. Teeth the fixes too. Every matrix cell must read ✓ before push.
4. Push once. Comment `@codex-review` once. Never re-comment without a new commit
   (PR 6 wasted 9 "no new commits" replies, PR 7 also 9).

### When a bot review lands

5. Run **address-pr** on the comment: verify the claim against local code, triage
   agree / push-back / clarify.
6. For accepted findings: smallest correct fix, plus a test that would have caught it.
7. **Chain completion (mandatory).** Treat the finding as one instance of a pattern and
   close the *whole* pattern in the same round — sibling routes/services/legacy copies,
   every cell of a guarantee matrix, the full new-field lifecycle, the honest path the
   closure might break, CI-blocking coverage for the new regression test.
   **No partial-defer.** A security/authz chain is either closed in this PR or the PR is
   not ready. Never push the "state-change half" and punt the "authorization half" to an
   ADR while the PR is open — the bot re-flags the deferred cell every push. PR 7 rounds
   22–24 are the same anonymous-write class, re-found each round after it was deferred to
   ADR 0010. If a cell is genuinely out of scope (pre-existing brownfield debt on a route
   this PR merely touches), fence it in the PR body with a tracking ref so it is answered
   "pre-existing, tracked in #X, out of scope" ONCE, not re-litigated every round.
8. Re-read the fix diff cold (address-pr Phase 4).
9. Optionally run a full teeth pass scoped to the fix diff.
10. Get commit approval, push.
11. Comment `@codex-review` — only ever after a push with new commits.

Repeat 5–11 until `Verdict: approve`. The bot does converge (PR 2 round 16, PR 3
round 26); the goal of this playbook is convergence in 2–3.

## Chain-completion rules


When a finding or fix touches one of these, finish the whole chain in one push:

1. **Cross-cutting guarantee matrix** (idempotency, rate limits…):
   every entrypoint that can trigger the guarded action
   × key/identity scoping (per-resource, not global)
   × payload binding (same key + changed payload → 409, not stale replay)
   × concurrency (insert races, double-fire).
   PR 7 spent 5 rounds on one cell each of the idempotency matrix.
1a. **Authz matrix (its own chain — PR 7 spent NINE rounds here, the single biggest leak):**
   every entrypoint that reads or writes regulated/money data
   (`/decision`, `/los/*`, offer, decision record, `run_decision`, boarding, remediation routes)
   × caller identity verified server-side (a spoofable header like `X-User-Id` is NOT auth)
   × the gateway proxy does not forward it anonymously (`/los/*`, `/decision` historically forward with no auth)
   × ownership scoping (a record endpoint must not answer for any caller's `app_id`).
   Same shape as the idempotency matrix — enumerate every cell across all 7 services + the gateway in ONE push.
2. **New-field lifecycle ladder:** no fabricated defaults → required at the API boundary
   (422 on missing; explicit 0 ≠ NULL) → persisted NULL/legacy rows fail closed → seed
   data backfilled → remediation path for stranded rows → remediation reachable through
   the gateway → frontend captures it. PR 7 spent 5 rounds climbing this one rung at a time.
3. **Parity sweep:** any fix to a route/service/config has siblings — the twin route
   (officer vs assistant), the other services' copies (redactor, config fallbacks), and
   legacy paths (servicing's old payment route). Grep all 7 services + legacy paths for
   the same shape before pushing.
4. **Trust-boundary completeness:** validating some model/user-controlled fields is not
   validating the output. Free text (summaries, narration) counts; structured-fields-only
   gates always get flagged.
5. **Front-door reachability:** the gateway proxy forwards only the verbs it forwards
   (historically GET+POST on `/los`). Every new route/verb needs a gateway-level test,
   not just service-direct tests.

## Filter-bypass enumeration

For any pattern-based guard (redaction, PII masking, secret scanning) — 40+ review
rounds of history on PRs 2–3 alone — enumerate before the first push:

- separators, and quoted separators
- URL-encoding; escape/unescape order (the guard must run AFTER unescaping)
- digits adjacent to the pattern
- value split across params/fields
- keys vs values (PII as JSON keys, query-param keys)
- numeric coercion artifacts (leading-zero SSN)
- partial fragments (last-four)
- label variants (tax_id / EIN / TIN spellings)
- lowercase / no-whitespace forms
- the malformed-input fallback path
- every alternate entrypoint of the public API (each param: history, role, metadata)
- every output surface: app log, access log, URLs, whole-payload logging services,
  telemetry/tracing sinks

## Standing traps (flagged repeatedly across PRs)

1. **CI gating:** every new security/validation regression test must live in a
   **blocking** CI job. The backend matrix runs with `|| true` — a test there proves
   nothing. Flagged on 4 separate PRs; check this first.
2. **Both directions:** after failing closed, prove the honest path still works in the
   same push — over-redaction corrupting facts, valid URL-encoded passwords rejected,
   migrations stranding legacy rows. "Your closure broke the legit case" is the
   predictable next round.
3. **Templates and defaults are part of the control:** `.env.example`, empty-string
   defaults, silent stub fallbacks. Presence ≠ validity — reject any-non-empty keys,
   placeholder passwords, nonsensical numeric config.
4. **The gate must hold against itself:** run any new CI gate against the branch that
   introduces it (PR 4's history scan failed on its own branch).
5. **New external sink** (LLM provider, tracing, telemetry) inherits the full redaction
   obligation — including error/validation-failure paths and caller-supplied metadata.
6. **Docs claims must match branch evidence:** no present tense for unbuilt work, no ADR
   "Accepted" over unverified citations, name sign-off owners for regulated contracts.
   (The bot runs a separate "Rescue review" mode on docs-only PRs.)

## Where this lives in tooling

- `docs/build-invariants.md` — the **shift-left** companion. This playbook makes the
  reviewer converge; build-invariants stops the finding from being writable. When a fix
  round here exposes a whole subsystem hardened one property at a time (PR 7's continuation
  token: ~13 rounds), that subsystem's kind belongs in build-invariants so the next feature
  builds it correct-by-construction. Kinds not covered by the chain-completion rules above:
  ephemeral-credential, dual-store-atomicity, replay-determinism, crypto-material.
- `.claude/skills/teeth/SKILL.md` — Phase 2.5 "Reviewer-preempt gauntlet" (this catalog
  as mandatory probe dimensions) + rule to teeth every fix commit before push.
- `.claude/skills/address-pr/SKILL.md` — Phase 3 step 6: mandatory chain completion
  during fix rounds.
