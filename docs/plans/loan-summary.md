# Plan — loan-summary route (owed item 2)

**Status:** PLAN READY — implementation scheduled later this week (not started). This document is the
acceptance block for the eventual PR body.
**Owed item:** #2 of the weeks 1–4 closure nine — "Loan summary gets a route. Done when: an officer
can open an application and see the summary."
**Scope:** thin slice. Application summary ONLY. Policy-Q&A (the other unused LLM surface) is a
separate, non-owed feature — explicitly out of this plan.
**Branch (planned):** `feat/loan-summary`, cut from `main`. Not a weekly deliverable, so no `-week`
suffix.

---

## Context

`services/origination-service/app/prompts/loan_summary.py` registers the `loan_application_summary`
prompt (version 2026-07-07) and `llm/client.py:219` wraps it as `summarize_application`. Every caller
is in `tests/test_llm_client.py` — no HTTP route, no UI. `get_llm_client`'s own docstring
(`main.py:67`) says "for routes that summarize via the LLM." The dependency was written for this and
never used. This is the client's first ask, with no surface.

## Preconditions — now met (were open when the original plan was drafted)

The original plan (2026-08-05) sequenced around two unmerged PRs. Both have since merged, which
removes its biggest blocker:

- **#11 merged** (`b9bfdc1`). `frontend/app/underwriting/[appId]/page.tsx` on `main` now carries the
  `routeGenRef` route-generation guard (`:180`) and the per-appId state-reset effect (`:210–222`), and
  the repo has the vitest harness + blocking `frontend` `npm test` job. So the branch is cut straight
  from `main` — no PR-#11 stacking, no re-deriving the guard, no lockfile-conflict risk. The old
  "Option A/B/C" branching analysis is moot.
- **`test_prompt_contracts.py` now tracked on `main`** (landed via #12). The registry-wide invariant
  test the plan relies on — every prompt's template must name its own `OUTPUT_SCHEMA` required keys —
  exists. This is the guard against the Week-4 `disclosure_narrate` failure (schema required keys the
  template never named, model invented its own, route 503'd). `loan_summary.py` USER_TEMPLATE names all
  three keys (`:46`), so that failure mode does not apply here, and the test enforces it.

## Locked decisions (unchanged)

- **GET**, not POST — nothing is recorded; sibling `GET /assistant/decisions/{app_id}` is precedent for
  an LLM read. Gateway proxy already accepts it (`methods=["GET","POST"]`), `apiGet` sends
  `cache: "no-store"`.
- **Officer-only** (`authz.require_officer(x_user_role)`), not officer-or-owner. `recommended_next_step`
  carries `decline_review` / `manual_underwrite`; a borrower must never see internal triage language
  about their own file.
- **`summary_payload` selects zero identity columns.** `redact_json` would mask them anyway (the prompt
  declares `json_vars=("application_json",)`), but not selecting them is least privilege and cheaper;
  redaction stays defense-in-depth, not the control.
- **No `fallback=` kwarg** on `summarize_application` — one success shape; the UI says "unavailable" off
  the 503.
- **No gateway change** — `/los/{path:path}` (`services/gateway/app/main.py:277`) already proxies it and
  sets `X-User-Role` from the session.
- **Button-triggered only** — see gap 5.

## Verified facts (checked against `origin/main`, 2026-08-06)

Columns `summary_payload` needs — all present, all non-identity:

| Source | Columns | Join |
|---|---|---|
| `applications` | `amount, term_months, purpose, income, monthly_debt, employer, job_title, employment_years, status` | base row `WHERE id = %s` |
| `offers` | `apr, finance_charge, monthly_payment, amount_financed, total_of_payments` | `LEFT JOIN … ON app_id` (may be absent) |
| `decisions` | `outcome` | `LEFT JOIN … ON app_id` (may be absent) |
| `kyc_checks` | `name_verified, dob_verified, address_verified, ssn_verified` | via `applications.applicant_id`, latest row |

Identity columns to **never** select: `applicants.name / dob / ssn / address / is_entity`.
`summary_payload` does not join `applicants` at all — identity is unreachable by construction, not just
filtered.

Current anchors on `main` (line numbers drift as files change — re-grep at implementation, do not trust
these blind):

- `prompts/loan_summary.py:11` OUTPUT_SCHEMA (required `summary`, `risk_flags`, `recommended_next_step`);
  `:46` USER_TEMPLATE names all three keys.
- `llm/client.py:219` `summarize_application`; `:106` `complete` (has `fallback=` — do not use).
- `main.py:66` `get_llm_client` (503 when `LLM_ENABLED` unset); `_run_assistant` 503-mapping pattern to
  mirror for provider failure.
- `routers/applications.py:484` `decision_request_payload` — the precedent for a payload builder in this
  file consumed by another module; **do not reuse it** (it joins `applicants` and selects
  `name/dob/ssn/address`, and 422s on NULL `monthly_debt`).
- `llm/adapter.py:210` `FakeAdapter` — the test double.
- `page.tsx:180` `routeGenRef`; `:210–222` per-appId reset; `:290–293` the on-mount `useEffect` (see
  gap 5).

## Files (gaps 3–8 folded in)

1. **`services/origination-service/app/routers/applications.py`** — add `summary_payload(app_id)` beside
   `decision_request_payload` (`:484`). Selects the verified column set above; joins `offers`,
   `decisions`, `kyc_checks`; **never joins `applicants`**. No 422 on NULL `monthly_debt` (a summary is
   advisory — omit the key, let the prompt's "summarize ONLY facts present" rule handle it). Returns
   `None` when the application does not exist.

2. **`services/origination-service/app/main.py`** — `GET /applications/{app_id}/summary`, declared here
   (not in the router) because `get_llm_client` lives in this module and importing it into the router is
   circular.
   - `authz.require_officer(x_user_role)`.
   - `Depends(get_llm_client)` → 503 when `LLM_ENABLED` unset (already handled).
   - `summary_payload(app_id) is None` → 404.
   - `client.summarize_application(json.dumps(payload))`; provider/adapter failure → **503 "summary
     unavailable"**, mirroring the `_run_assistant` 503 map. No `fallback=`. No idempotency key.
   - Response is the validated dict `complete()` returns — `summary`, `risk_flags`,
     `recommended_next_step`.

3. **`frontend/app/underwriting/[appId]/page.tsx`** — new "Application summary" card. **[gap 3]** The
   file now has three panel areas (decision, assistant, disclosure); decide placement among all three —
   default above the Decision panel (triage input, read before deciding). Fold into the existing shape:
   - add `summary` state to the per-appId reset block (`~:217–222`, beside `setDisclosureDoc(null)`);
   - guard the fetch with `routeGenRef` (`:180`) — capture the generation, drop late responses;
   - reuse `actionBusy / actionErr / errMsg`; a **"Summarize" button**.
   - **[gap 5]** Button-triggered ONLY. Do **not** add the summary fetch to the on-mount `useEffect`
     (`:290–293`) that `loadDisclosure` uses — a GET is a paid provider call, so on-mount would bill
     every application open. This is the one wrong pattern sitting right next to the right one.
   - Render summary prose, `risk_flags` as amber chips, and `recommended_next_step` under an explicit
     "triage hint from the assistant, not a lending decision" line — it must not read as a second
     decision outcome next to the record-backed one.

4. **`services/origination-service/tests/test_summary_route.py`** (new) — FastAPI `TestClient` +
   `FakeAdapter`, following `tests/test_assistant.py`:
   - happy path returns all three schema keys;
   - **the test that carries weight:** `summary_payload` output contains no identity keys — assert
     `name / ssn / email / phone / address / dob` absent from the built payload;
   - borrower role → 403; unknown app id → 404; `LLM_ENABLED` unset → 503; adapter raising → 503;
   - a NULL `monthly_debt` application still summarizes (no 422).

5. **`frontend/app/underwriting/[appId]/page.test.tsx`** — one added assertion that `summary` clears on
   an appId change, extending the existing per-route state-leak tests. **[gap 1 resolved]** No longer
   conditional on #11 — the harness is on `main`; the blocking `frontend` `npm test` job runs it.

## Cut from this slice

Spec doc, ADR, teeth pass. Streaming (`client.stream` deliberately raises until buffer-then-validate
exists). Caching or persisting summaries. Auto-summarize on load. A borrower-facing summary. Any
`fallback=` use. Policy-Q&A (separate feature).

## Verification

1. `cd services/origination-service && python -m pytest tests/test_summary_route.py -q`, then the full
   service suite.
2. **`make prove` in a detached clean worktree** — the working tree is chronically dirty and `make prove`
   aborts on it; never stash the user's work. **[gap 8 / new-feature semantics]** The test fails at the
   parent commit because the route does not exist (404 / import error), not because of wrong behaviour —
   still a real prove: it shows the test exercises the new code and cannot pass without it. Say so in the
   PR body so it does not read as a fake pass.
3. `make up` with `CLAUDE_API_KEY` exported in the host shell (compose passes it through; never written
   to `.env`).
4. Log in at `http://localhost:3000` as `underwriter / password`, open an application, click Summarize.
   Confirm the summary references no applicant name, and **no request fires on page load** — only on the
   click.
5. Navigate A → B mid-request; confirm A's summary never appears under B's header.
6. `curl` the endpoint with the borrower `maria` session through the gateway → 403.
7. Restart without `LLM_ENABLED` → the button surfaces "unavailable", not a 500.
8. **[gap 8 / demo traceability]** "Done when the officer opens an application and *sees* the summary" =
   after a Summarize click. For the Monday demo the route must be **on `main`** to count (deck rule: no
   slide off an unmerged branch).

## Before the first edit

- Settle/confirm the branch: cut `feat/loan-summary` from `main`; confirm the working tree does not carry
  the summary uncommitted onto the wrong branch.
- **[gap 6 — done]** Columns verified above; no schema change, so no migration and no new DB-readiness
  rung.
- **[gap 3/4]** Re-grep the `page.tsx`, `main.py`, `client.py` anchors before editing — line numbers here
  are a 2026-08-06 snapshot.
