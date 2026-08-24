---
name: address-pr
description: Analyze and resolve review feedback against the local feature branch on the meridian-lending project. Two entry modes. (1) Pasted mode — the user pastes review comments (from a PR, the teeth skill, Claude's own review, or written by hand) that need to be triaged and fixed. (2) Poll mode — the user provides a PR URL and the skill polls that PR every 30 minutes via read-only HTTPS GETs to the GitHub REST API (no auth for public repos; gh only if private) for new review comments, then automatically triaging and fixing them (only the commit/push step still needs the user's approval). In both modes the skill never POSTS or writes to GitHub/GitLab (no comments, no PR edits) — it only ever reads the PR; all fixing happens on the local git feature branch: it diffs the branch against its base (main) for context (widening to the rest of the repo only when a comment requires it), verifies each comment against the actual local code, analyzes each on its merits, decides agree/push-back/clarify, then implements the accepted fixes and reports what it did. Trigger on phrases like "address these PR comments", "resolve this review feedback", "work through these comments", "apply these review fixes", or "poll this PR for new comments".
---

# Address PR: Analyze & Resolve Pasted Review Feedback Against the Local Branch

You take review comments and turn the valid ones into correct, implemented fixes on the current local feature branch. Comments reach you two ways: the user pastes them (pasted mode), or you fetch them read-only from a PR URL the user gives you (poll mode — see Phase 6). You NEVER write to GitHub/GitLab: no posting comments, no editing the PR, no closing/merging. The only remote call you ever make is a read (unauthenticated HTTPS GET to the GitHub REST API; gh only if the repo is private) to list a PR's comments and state in poll mode. Every comment is verified against the real local code, not against what the comment claims. You are not a yes-machine: a comment is an input to judge, not an order to obey. Some are right, some are wrong, some are stale (the code already changed), some are ambiguous. Your job is to tell them apart, act on the good ones, and push back on the bad ones with evidence from the actual code.

## Worktree setup (always runs first — before Phase 0, both entry modes)

Every fix round happens in a dedicated git worktree, never in the main checkout. This replaces branch-switching for this skill: it satisfies the global branch-discipline rule (never edit-then-switch) by isolating instead of switching.

1. Target branch is the branch the user names when invoking the skill (pasted mode: given with the comments; poll mode: the PR's head branch, read from the API). Required — do not guess or default to whatever the main checkout happens to be on.
2. Path convention: `.claude/worktrees/<branch-with-/-replaced-by-->` (matches EnterWorktree's own default location).
3. Check `git worktree list` first:
   - A worktree already at that path for that branch → reuse it: `EnterWorktree` with `path:<that path>`.
   - No worktree yet → create one for the EXISTING branch (not a new one — `EnterWorktree` with only `name` branches fresh from origin/main, which is wrong here): run `git worktree add .claude/worktrees/<slug> <branch>` directly, then `EnterWorktree` with `path:<that path>` to switch the session into it.
   - `git worktree add` refuses if the branch is already checked out somewhere (commonly the main checkout, if the user is currently sitting on it there). Do not force or move the other checkout yourself — report this to the user and ask whether to switch the main checkout off that branch first, or proceed in the main checkout for this one round.
4. If EnterWorktree/ExitWorktree aren't available in this session, fall back to plain `git worktree add` + operating with `cwd` pinned to that path, and tell the user there is no auto-tracked cleanup this run.
5. Do the rest of the skill (Phases 0–6) inside the worktree. Do not `git checkout` the target branch in the main working directory for any part of this skill.
6. Leave the worktree in place at the end of the round (don't ExitWorktree/remove it) regardless of whether Phase 5 pushed — so a declined or partial round stays inspectable. Only remove it on explicit user request.

## Phase 0 — Gather inputs (comments are pasted, code is local)

1. Take the review comments exactly as the user pasted them. Do not fetch anything from a remote. The source may be a PR, the teeth skill, Claude's own review, or hand-written — note the source per comment but treat them all as claims to verify.
2. Identify the current local feature branch (now the worktree's branch, per Worktree setup above) and its base (e.g. git branch --show-current, and the merge-base against the base branch main), unless the user names a different one. Default the base to main for this project; only ask if the user indicates a different base.
3. Normalize the pasted comments into a numbered list so nothing is silently dropped. Every comment gets an explicit disposition by the end.

## Phase 1 — Load context from the local branch (diff first, widen only as needed)

1. Build the working set from the local diff: git diff main...HEAD (main is the base branch for this project) and the files it touches. That is your default scope — do not pull in the whole repo up front.
2. VERIFY each comment against the actual local code before judging it. A comment may be stale — the line it references may already have been changed, moved, or deleted on this branch. Open the real file at the real current location; never trust the comment's description of the code over what the code actually says.
3. Widen context beyond the diff ONLY when a specific comment requires it (a contract/interface used elsewhere, a shared util, a migration's effect on other callers, whether a change breaks an existing test). When you widen, say which comment forced it and what you looked at.
4. Build an end-to-end understanding of the feature before changing anything: what it does, the request/data path, the trust boundaries the changes sit on.

## Phase 2 — Analyze each comment (decide before you touch code)

For every comment, assign one disposition:

- ACCEPT — correct, actionable, and still applies to the current code. Note the fix you intend.
- PARTIAL — real issue but the suggested fix is wrong/incomplete; fix the underlying problem differently. Explain the divergence.
- REJECT — mistaken, based on a misreading, or would introduce a bug/regression. Give a concrete reason with evidence from the local code.
- STALE — the comment no longer applies because the branch already addresses or removed the code in question. Show what in the current code makes it moot.
- CLARIFY — genuinely ambiguous or depends on intent you can't infer. State the specific question; do NOT guess and implement.

Rules:
- Judge on technical merit against the real code, not on politeness and not on the comment's own framing.
- Surface comments that conflict with each other instead of silently picking one.
- Watch for fixes that satisfy a comment but break something the diff doesn't show — that's when you widen context.
- A fix for one comment can invalidate another; sequence them and note dependencies.

## Phase 3 — Implement the accepted fixes

For ACCEPT and PARTIAL items only:
1. Make the smallest change that correctly resolves the issue. Don't refactor unrelated code or expand scope.
2. Preserve existing behavior outside the fix; don't introduce new failure modes.
3. If the change touches a contract/interface, update all affected callers found when widening context. A half-applied fix is worse than none.
4. Add or update tests when the comment was about a correctness or edge-case gap. A bug fix without a test that would have caught it is incomplete.
5. After each fix, re-check pending comments — confirm this change didn't invalidate or partially resolve another.
6. CHAIN COMPLETION (mandatory — this is what collapses review round-trips): a reviewer finding is usually one instance of a pattern, and the automated reviewer surfaces only 1–2 findings per pass, so fixing the single instance guarantees another round. Before Phase 4, run the fix through the teeth skill's "Phase 2.5 — Reviewer-preempt gauntlet" checklist (chain-completion rules, filter-bypass enumeration, standing traps) and close the WHOLE pattern in this same fix round: all sibling routes/services/legacy copies with the same shape, all cells of a guarantee matrix (entrypoints × scoping × payload binding × concurrency), the full new-field lifecycle (boundary → legacy rows → seeds → remediation → gateway reachability), the honest path a new fail-closed check might break, and CI-blocking coverage for any new security regression test. Each closed pattern-mate is in scope for the fix commit, not scope creep. For a guarantee/authz matrix, emit the filled coverage table (entrypoint × cell, ✓/✗) — an unchecked cell is the next round's finding; close it now.
   NO PARTIAL-DEFER of a security chain: never fix the "state-change half" and punt the "authorization half" to an ADR while the PR is open — the reviewer re-flags the deferred cell every push (PR 7 rounds 22–24 were the same anonymous-write class deferred to ADR 0010 and re-found each round). Close the whole cell now, or if it is genuinely pre-existing brownfield debt on a route this PR only touches, fence it in the PR body with a tracking ref so it is answered once. If the user directs a partial-defer, warn that it guarantees another round.

Do NOT implement REJECT, STALE, or CLARIFY items.

## Phase 4 — Verify against the branch

1. Re-read the diff you produced (git diff) as if reviewing it cold. Does each change actually do what the comment asked?
2. Run or describe the tests covering the changed paths. If you can't run them, say so and say what should be run.
3. Confirm: every ACCEPT/PARTIAL is fully implemented, no REJECT/STALE was silently implemented, and the working tree is in a coherent state.

## Phase 5 — Commit & push the fixes

Only after Phase 4 verification passes, and only for changes made by this skill run.

ASK FIRST: before staging anything, show the user the proposed commit message(s) and the file list per commit, and ask for explicit approval to commit and push. Do not commit or push until the user confirms. If the user declines, leave the working tree as-is and stop after the report.

Present the proposed commit message as plain text — do NOT wrap it in a fenced code block and do NOT use backticks, bold, or any other markdown inside the message itself. The message must be copy-pasteable straight into GitHub's commit/PR message window without mixed fonts or stray formatting characters.

Once approved:

1. Stage exactly the files you changed for the accepted fixes (`git add <specific files>` — never `git add -A` / `git add .`).
2. Commit with a message formatted for GitHub's commit message window:
   - Subject line: imperative mood, conventional-commit type (`fix:`, `refactor:`, `test:`, `chore:`), ≤ 72 characters (aim ≤ 50), no trailing period. GitHub truncates anything longer with `…`.
   - Blank line after the subject.
   - Body: wrap lines at 72 characters. State which review comments the commit resolves and why the fix is correct — one bullet per addressed comment, e.g. `- resolves review comment 2: null check on borrower SSN before redaction`. Accurate to what actually changed; never claim a comment is fixed if it was REJECT/STALE/CLARIFY.
   - Plain text only inside the message: no markdown, no backticks, no code fences — GitHub's message window renders it as plain text and formatting characters show up literally.
   - Never write review-comment numbers as `#N` (write `comment 2`, not `comment #2`). GitHub autolinks every `#N` in a commit message to issue/PR number N in the repo, and the patterns `fixes #N` / `closes #N` / `resolves #N` will auto-close that issue when the commit reaches the default branch. Only use `#N` when you genuinely intend to reference (or close) that GitHub issue/PR.
   - Group related fixes into one commit; use separate commits when fixes are logically independent (e.g. a code fix vs. an unrelated test gap).
   - No Co-Authored-By or other trailers.
3. PROVE THE REGRESSION TEST (mandatory whenever the fix commit added or changed a test): run `make prove` on the new commit (it proves HEAD; use `make prove REF=<sha>` for an earlier one). It rolls that commit's source back to the parent and requires the test to FAIL without the fix and PASS with it. If it prints REJECTED, the test does not actually catch the bug — return to Phase 3, fix the test, amend or add a commit, and re-run until it prints PROVEN. Never push a fix whose regression test you have not watched fail first. If the round changed no test (a REJECT/STALE-only round, or a pure-refactor fix with no behavioral change), `make prove` is not applicable — say so and skip it.
4. OPTIONAL ADVERSARIAL CHECK: after the commit completes and BEFORE pushing, ask the user once whether they want an adversarial check on the fix — offer to run the teeth skill scoped to the review-fix diff. This is strictly optional and entirely the user's choice; declining is a normal path, not a warning condition. Do not run it, skip it, or push for it on your own, and do not re-ask after a "no".
   - If the user says yes: invoke the teeth skill on the fix commit's diff. If it surfaces real problems, address them (back through Phase 3/4), commit the corrections, then offer the check again for the new corrections only.
   - If the user says no: proceed straight to push with no further mention.
5. Push to the remote tracking branch: `git push` (or `git push -u origin <branch>` if no upstream is set). Plain `git push` on the existing feature branch only — never force-push, never push to main, and do not open/edit the PR itself (updating the PR happens automatically because the branch is already associated with it).
6. If the push fails (auth, diverged remote), report the exact error and stop — do not rebase or force anything without the user.
7. After a successful push, produce a paste-ready PR reply for the user to post in the GitHub PR conversation window (the skill does not post it — the user pastes it). Format rules:
   - Plain, uniform-font text: no backticks, no code fences, no inline code, no blockquotes, no tables. GitHub PR comments render markdown, and any backtick switches to a monospace font mid-sentence — avoid that entirely. Simple dash bullets and blank lines between sections are the only structure allowed.
   - Write file paths and function names as bare text (services/loan-service/handler.js), not wrapped in backticks.
   - Open with one line saying fixes are pushed and the short commit SHA(s), e.g. "Pushed review fixes in a1b2c3d."
   - Then one dash bullet per addressed comment, led by the comment's TOPIC in a few words (the concern it raised — e.g. "Raw PII key in report:"), not a number: what was changed, where (file and function in plain text).
   - Then pushbacks/stale/clarify items, each as its own bullet with the concrete reason or the direct question — polite, evidence-based, no hedging.
   - KEEP IT SHORT AND CRISP. One line per bullet, ~1 sentence. State what changed and where — cut restated problem descriptions, mechanism walk-throughs, and "as you noted" framing. The reviewer knows the comment; say what you did about it. No preamble, no sign-off.
   - Do NOT number comments (no "Comment 1", "comment 2") — numbering is a per-session triage artifact that drifts when work resumes in a new session, so it means nothing to the reviewer. Identify each by topic. Autolink caution still holds: never write "#N" (it autolinks to issue/PR N).
   - Present this reply to the user as plain text exactly as it should be pasted — not wrapped in a code block.

Example PR reply (show it to the user exactly like this):

Pushed review fixes in a1b2c3d.

- Loan term bounds: reject term <= 0 with a 400 before the amortization calc in services/loan-service/handler.js.
- Zero-term test gap: added a zero-term unit test in loan-service handler tests.
- Cache invalidation: not applied — the existing TTL expiry path already covers this; adding it would double-invalidate. Happy to walk through it.

Example commit message (show it to the user exactly like this — plain text, no code fence around it):

fix: validate loan term bounds before amortization calc

- resolves review comment 1: reject term <= 0 in loan-service handler
- resolves review comment 3: add unit test for zero-term edge case

Review comment 2 rejected: suggested cache invalidation already
handled by the existing TTL path (see ledger).

## Phase 6 — Poll mode (optional: watch a PR for new review comments)

Enter this phase ONLY when the user gives you a PR URL and asks to poll/watch it (e.g. "poll this PR for new comments"). Pasted mode (Phases 0–5) is unaffected. Poll mode is read-only against the remote: you fetch comments and PR state with plain HTTPS GETs, and you NEVER post, edit, close, or merge. Run Worktree setup once at the first poll cycle (the PR's head branch is the target branch) — every subsequent auto-triage cycle (6.3) reuses that same worktree.

Prerequisites: none beyond network access for a public repo — the GitHub REST API serves public PR reads unauthenticated (rate limit 60 req/hour per IP; one poll is ~3 calls, so 30-minute polling stays well under it). Use `curl` against `https://api.github.com`. Only reach for `gh` (which needs auth) if a GET returns 404/403 indicating the repo is private; if so, tell the user auth is required (`gh auth login`) and stop.

Scheduling mechanism (what makes it recurring): the automatic 30-minute cadence is realized by the `ScheduleWakeup` tool, which is the dynamic-pacing primitive of the loop skill — it only fires when THIS session is running under a loop (e.g. the user launched via `/loop 30m /address-pr <url>`, or a loop is otherwise active). If `ScheduleWakeup` is not available in the session, poll mode is MANUAL: run exactly one cycle (6.1) each time the user re-invokes the skill on the PR URL, skip the auto-reschedule in 6.2, and tell the user to re-run when they want the next check. Do not claim a background timer is running when no scheduling primitive is present.

State file (tracks which comments you have already surfaced, so a re-poll shows only new ones):
`<scratchpad>/address-pr-seen-<pr-number>.txt` — one comment id per line. `<scratchpad>` is the session scratchpad directory supplied by the harness (its absolute path is in the system prompt; substitute it, do not write the literal token). It persists across the 30-minute wakeups within a looped session; in manual mode it persists for as long as the session lives, and the same seen-set logic still de-dupes across re-invocations.

### 6.1 — One poll cycle

1. Resolve owner/repo/number from the URL. Base = `https://api.github.com/repos/{owner}/{repo}`. Read state:
   `curl -s $BASE/pulls/{number}` → read `state` and `merged`.
2. If `state` is `closed` (merged or not): report it, do NOT reschedule, and stop poll mode (if running a dynamic loop, call ScheduleWakeup with stop:true). This is the normal termination.
3. Fetch all current review feedback (both kinds), read-only:
   - Diff-line review comments: `curl -s "$BASE/pulls/{number}/comments?per_page=100"` → each has `id, user.login, path, line, body, created_at`.
   - Conversation comments: `curl -s "$BASE/issues/{number}/comments?per_page=100"` → each has `id, user.login, body, created_at`.
   - (Paginate if either returns exactly 100 — follow the `Link: rel="next"` header.)
   Drop comments authored by the PR author themselves (the user's own replies are not review feedback to act on) — key off `user.login`; the PR author is `pulls/{number}` field `user.login`.
4. FIRST CYCLE = baseline seed. If the state file does not exist yet, this is the first poll: write ALL current (non-author) comment ids to the state file and surface NOTHING. Report "baseline set — N existing comments recorded, watching for new ones" and reschedule (6.2). You are watching for comments that arrive AFTER polling starts, not re-triaging the existing thread.
5. Subsequent cycles: load the seen-id set from the state file. NEW = fetched (non-author) comments whose id is not in the set.
   - No new comments and PR still open → reschedule the next poll (6.2). Report "no new comments" briefly.
   - New comments found → append their ids to the state file, present them as a numbered list (same normalization as Phase 0 step 3: comment text, author, file/line), then AUTOMATICALLY proceed into Phases 1–5 to triage and fix them — do NOT wait for the user's go. The ONE approval gate that still stands is Phase 5: never stage, commit, or push without explicit user approval. Do not reschedule the next poll until the fix round finishes (or the user declines the commit).

### 6.2 — The 30-minute cadence

To realize the 30-minute interval, after a clean cycle (no new comments, PR open) schedule the next poll with ScheduleWakeup: `delaySeconds: 1800`, and a prompt that re-enters this poll on the same PR URL. One scheduled wakeup at a time — do not stack timers. Stop rescheduling the moment the PR is merged/closed (6.1 step 2) or the user says stop.

If ScheduleWakeup is unavailable (no active loop — see the Scheduling mechanism note above), do NOT pretend to reschedule: finish the cycle, tell the user the poll ran once and how to trigger the next one (re-invoke the skill on the same URL, or launch it under `/loop 30m` for hands-off polling), and stop. The seen-set state file already makes each manual re-run surface only genuinely new comments.

### 6.3 — Auto-triage the new comments

Take the surfaced new comments as the input to Phase 1 and run Phases 1–5 immediately, without waiting for the user (verify against local code, triage, fix accepted ones — then ASK before commit/push, the one gate that remains). These comments' ids are already in the seen set, so they will not resurface. After the fix round completes (or the user declines the commit), resume poll mode from 6.1 on the next 30-minute wakeup.

Two disposition paths need calling out in auto-triage mode, since "auto-fix" is not "auto-obey":
- All REJECT/STALE (no ACCEPT/PARTIAL in the batch) → nothing is changed, so Phase 5 never runs and NO commit-approval prompt appears. This is the correct terminal state, not a stall: produce the report + a paste-ready PR pushback reply (the user posts it), then reschedule the next poll (6.2). Do not commit or push — there is nothing to commit.
- Any CLARIFY → this is the one item that HALTS the auto-flow. Phase 2 forbids guessing, so you cannot resolve it unattended: stop, ask the user the specific question, and wait. Fix any ACCEPT/PARTIAL items in the same batch first if you can do so independently; only the CLARIFY item waits. Resume polling after the user answers.

## Output format

1. Comment ledger — numbered table: comment (short), source, disposition (ACCEPT/PARTIAL/REJECT/STALE/CLARIFY), one-line rationale grounded in the actual code. Covers every comment, none missing.
2. Changes made — per fix: which comment(s) it resolves, files/locations touched, what changed. Reference the diff rather than pasting large blocks.
3. Pushback, stale & clarifications — REJECT items with code-based reasons, STALE items with what makes them moot, CLARIFY items as direct questions.
4. Tests — what you added/updated and what you ran (or what still needs running).
5. Context widening log — any time you went beyond the diff, which comment drove it and what you checked.
6. Residual risk — anything uncertain, partially addressed, or worth a second look before merge.
7. Commit & push — whether the user approved; if yes, the commit SHA(s), message subject(s), and push result. If not approved or push failed, say so exactly.
8. PR reply — the paste-ready reply comment from Phase 5 step 7 (only when a push happened). Note whether the adversarial gate (teeth) was run or declined.

Be honest about what you could not verify or implement. Do not claim a comment is resolved unless the change in the local branch genuinely resolves it.
ENDOFSKILL