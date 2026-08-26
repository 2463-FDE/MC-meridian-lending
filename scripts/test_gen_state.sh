#!/usr/bin/env bash
# test_gen_state.sh — tests for scripts/gen_state.sh.
#
# The load-bearing cases:
#   `--check catches drift`   — without it the freshness gate could pass unconditionally
#      and docs/state.md would rot exactly like the hand-written page it replaced.
#   `no base ref aborts`      — a generator that cannot read git must exit 2, not print a
#      clean page. Exit 0 over an unread source is the failure this whole gate exists to
#      prevent, one level up.
#   `empty ledger is not an abort` — a truthful empty result must stay exit 0, or the
#      abort masks real findings.
#
# Usage: ./scripts/test_gen_state.sh    Exit 0 = all pass, 1 = a failure.
set -uo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/gen_state.sh"
[ -x "$SCRIPT" ] || { echo "ABORT: $SCRIPT not executable." >&2; exit 1; }

# Setup is checked, not assumed. A failed `mktemp` under a full or read-only TMPDIR alone leaves TMPROOT
# empty, every fixture path collapses to an absolute one, and the cases then run the
# real script against the REAL repo -- reporting "expected a failure, got OK" about a
# repo the test never built. Exit 2 = could not run, distinct from exit 1 = a case
# failed, the same split the two scripts under test use.
TMPROOT=$(mktemp -d) || { echo "ABORT: mktemp -d failed." >&2; exit 2; }
[ -n "$TMPROOT" ] && [ -d "$TMPROOT" ] || { echo "ABORT: mktemp -d produced no directory." >&2; exit 2; }
trap 'rm -rf "$TMPROOT"' EXIT
pass=0; fail=0

git_c() { git -C "$1" -c user.email=t@t -c user.name=t "${@:2}"; }

new_repo() {  # a repo with one merge commit naming a PR, one ADR, one workflow.
              # Prints the fixture path, or nothing and returns 1 if setup failed.
  local d; d=$(mktemp -d "$TMPROOT/repo.XXXXXX") || return 1
  [ -n "$d" ] || return 1
  git -C "$d" init -q -b main || return 1
  mkdir -p "$d/docs" "$d/adr" "$d/.github/workflows"
  echo "x" > "$d/seed"; echo "# adr" > "$d/adr/0001-first.md"
  printf 'jobs:\n  backend:\n    steps:\n      - run: pytest || true\n  secret-scan:\n    steps:\n      - run: ./scan.sh\n' \
    > "$d/.github/workflows/ci.yml"
  git -C "$d" add -A; git_c "$d" commit -qm init
  git -C "$d" checkout -q -b topic; echo y > "$d/f"; git -C "$d" add -A; git_c "$d" commit -qm work
  git -C "$d" checkout -q main
  git_c "$d" merge -q --no-ff topic -m "Merge pull request #7 from org/topic"
  printf '%s' "$d"
}

# run NAME EXPECTED_EXIT REPO PATTERN ARGS...
run() {
  local name=$1 want=$2 repo=$3 pattern=$4; shift 4
  # A fixture that failed to build must stop the run, not be graded. Without this the
  # empty repo path makes `cd ""` a successful no-op and the case grades the real repo.
  [ -n "$repo" ] && [ -d "$repo/.git" ] || { echo "ABORT: fixture repo missing — setup failed." >&2; exit 2; }
  local out got
  out=$(cd "$repo" && "$SCRIPT" "$@" 2>&1); got=$?
  if [ "$got" -ne "$want" ]; then
    echo "FAIL  $name — exit $got, wanted $want"; printf '%s\n' "$out" | sed 's/^/        /'
    fail=$((fail + 1)); return
  fi
  if [ -n "$pattern" ] && ! printf '%s\n' "$out" | grep -qE "$pattern"; then
    echo "FAIL  $name — exit $got as wanted, but output lacks /$pattern/"
    printf '%s\n' "$out" | sed 's/^/        /'; fail=$((fail + 1)); return
  fi
  echo "ok    $name"; pass=$((pass + 1))
}

# --- 1. it generates, and the generated page carries the derived facts -------
r=$(new_repo)
run "writes state.md" 0 "$r" 'OK: wrote docs/state.md'
for want in 'Merge pull request #7' '\| #7 \|' 'adr/0001-first.md' 'Count: 1' \
            '\*\*soft\*\* — `backend`' '\*\*BLOCKING\*\* — `secret-scan`'; do
  if grep -qE "$want" "$r/docs/state.md"; then echo "ok    generated page has /$want/"; pass=$((pass + 1))
  else echo "FAIL  generated page lacks /$want/"; fail=$((fail + 1)); fi
done

# --- 2. --check is clean right after a write ---------------------------------
run "--check clean after write" 0 "$r" 'matches the base' --check

# --- 3. --check catches a hand edit ------------------------------------------
# The whole point: docs/state.md must not be editable without CI noticing.
echo 'someone hand-edited this line' >> "$r/docs/state.md"
run "--check catches drift" 1 "$r" 'STALE:' --check

# --- 4. --check catches new git history the page has not absorbed ------------
r=$(new_repo); (cd "$r" && "$SCRIPT" >/dev/null)
git -C "$r" checkout -q -b topic2; echo z > "$r/g"; git -C "$r" add -A; git_c "$r" commit -qm w2
git -C "$r" checkout -q main; git_c "$r" merge -q --no-ff topic2 -m "Merge pull request #8 from org/topic2"
run "--check catches an unabsorbed merge" 1 "$r" 'STALE:.*|#8' --check

# --- 5. --check on a page that does not exist is a failure, not a pass -------
r=$(new_repo)
run "--check with no page fails" 1 "$r" 'does not exist' --check

# --- 6. a repo with no merge commit is truthfully empty, NOT an abort --------
# Aborting on an empty-but-successful read would mask real results.
d=$(mktemp -d "$TMPROOT/bare.XXXXXX"); git -C "$d" init -q -b main
echo x > "$d/seed"; git -C "$d" add -A; git_c "$d" commit -qm init
run "empty ledger is not an abort" 0 "$d" 'OK: wrote'
if grep -q 'No merge commit' "$d/docs/state.md"; then echo "ok    empty ledger says so"; pass=$((pass + 1))
else echo "FAIL  empty ledger did not say so"; fail=$((fail + 1)); fi

# --- 7. no base ref at all must abort, never print a clean page --------------
d=$(mktemp -d "$TMPROOT/nomain.XXXXXX"); git -C "$d" init -q -b other
echo x > "$d/seed"; git -C "$d" add -A; git_c "$d" commit -qm init
run "no base ref aborts" 2 "$d" 'ABORT: neither main nor origin/main'

# --- 8. usage error aborts ---------------------------------------------------
r=$(new_repo)
run "bad flag aborts" 2 "$r" 'usage:' --wat

# --- 9. the CI job list comes from HEAD, not from the merge base -------------
# The page names which jobs in ci.yml block. Reading that list from the merge base
# means a PR that ADDS a blocking job cannot represent it: the committed page still
# ends at the old list, and the first regeneration after the merge -- when the base
# has moved forward -- reports drift on output nobody could have generated. History
# (tip, ledger, ADRs) stays on the merge base; the workflow is a property of THIS
# tree, so it is read from HEAD.
r=$(new_repo)
git -C "$r" checkout -q -b topic3
printf 'jobs:\n  backend:\n    steps:\n      - run: pytest || true\n  secret-scan:\n    steps:\n      - run: ./scan.sh\n  kb-freshness:\n    steps:\n      - run: ./scripts/gen_state.sh --check\n' \
  > "$r/.github/workflows/ci.yml"
git -C "$r" add -A; git_c "$r" commit -qm "add a blocking job"
run "generates on a branch that adds a job" 0 "$r" 'OK: wrote docs/state.md'
if grep -qE '\*\*BLOCKING\*\* — `kb-freshness`' "$r/docs/state.md"; then
  echo "ok    branch-added job reaches the page"; pass=$((pass + 1))
else echo "FAIL  branch-added job missing from the page"; fail=$((fail + 1)); fi
# and the base tip stays the MERGE BASE, so a sibling merge does not flap the page.
if grep -qE 'Base: `main \(' "$r/docs/state.md" && ! grep -q 'add a blocking job' "$r/docs/state.md"; then
  echo "ok    base tip still the merge base"; pass=$((pass + 1))
else echo "FAIL  base tip moved off the merge base"; fail=$((fail + 1)); fi

# --- 10. on main the page can never be clean, which is why CI skips push ------
# On main the merge base IS HEAD, so committing the generated page moves the base to
# the commit that carries it and the next --check reports drift on a page nobody can
# fix. That is structural, not a bug in the page: ci.yml therefore runs kb-freshness
# on pull_request only. This test pins the reason so the `if:` is never "cleaned up".
r=$(new_repo); (cd "$r" && "$SCRIPT" >/dev/null)
git -C "$r" add -A; git_c "$r" commit -qm "docs: regenerate state"
run "committing the page on main re-stales it" 1 "$r" 'STALE:' --check

# --- 12. a merge-ref checkout grades against main's TIP, not the branch base ---
# Characterization of git, and the reason ci.yml must pin the PR head sha.
# `actions/checkout@v4` on a pull_request checks out refs/pull/N/merge -- the branch
# merged with the base tip -- and the base tip is then an ANCESTOR of HEAD, so the
# merge base collapses to that tip. Same commits, different checkout shape, opposite
# verdict: clean at the branch head, STALE under the merge ref. That is how a sibling
# PR merging turned every other open PR red while its page was correct.
r=$(new_repo)
git -C "$r" checkout -q -b topic4; echo w > "$r/w"; git -C "$r" add -A; git_c "$r" commit -qm work
(cd "$r" && "$SCRIPT" >/dev/null)   # page generated at the branch base
git -C "$r" add -A; git_c "$r" commit -qm "docs: regenerate state"
run "clean at the branch head" 0 "$r" 'OK: docs/state.md matches' --check
# a sibling merges into main, the branch is untouched
git -C "$r" checkout -q main; git -C "$r" checkout -q -b sibling
echo s > "$r/s"; git -C "$r" add -A; git_c "$r" commit -qm sibling
git -C "$r" checkout -q main
git_c "$r" merge -q --no-ff sibling -m "Merge pull request #8 from org/sibling"
git -C "$r" checkout -q topic4
run "branch head still clean after a sibling merge" 0 "$r" 'OK: docs/state.md matches' --check
# now the merge-ref shape CI actually checks out
git -C "$r" checkout -q -b prmerge topic4
git_c "$r" merge -q --no-ff main -m "Merge main into topic4 (simulated refs/pull/N/merge)"
run "merge ref grades against the tip" 1 "$r" 'STALE:' --check

# --- 13. ci.yml pins the PR head sha for kb-freshness -------------------------
# The defect above lives in the WORKFLOW, not the generator, so a case against the
# script alone cannot catch a revert of the one line that fixes it. Grade the real
# ci.yml: kb-freshness must check out the PR head, or its merge-base grading is the
# tip grading and the job flaps on every sibling merge.
# checkout_ref FILE — print the `ref:` VALUE of the kb-freshness job's actions/checkout
# step, or print nothing and return 1 when the job, the step or the key is absent.
#
# Structural, not textual. Grepping the job block as raw text passes on the string
# appearing ANYWHERE in it -- including a comment that survives while the real `ref:`
# key is dropped, which is the one edit most likely to reintroduce the merge-ref
# grading. Comments are stripped before the keys are read, so only the value counts.
checkout_ref() {
  local f=$1
  [ -f "$f" ] || return 1
  local v
  v=$(awk '
    /^  kb-freshness:/                     { injob = 1; next }
    injob && /^  [A-Za-z0-9_-]+:/          { injob = 0 }
    !injob                                 { next }
    {
      line = $0
      sub(/^[[:space:]]*#.*$/, "", line)   # a whole-line comment
      sub(/[[:space:]]#.*$/,  "", line)    # a trailing comment
    }
    line ~ /^      - /                     { instep = (line ~ /uses:[[:space:]]*actions\/checkout/); next }
    instep && line ~ /^[[:space:]]+ref:[[:space:]]*/ {
      sub(/^[[:space:]]+ref:[[:space:]]*/, "", line)
      print line
      exit
    }
  ' "$f") || return 1
  [ -n "$v" ] || return 1
  printf '%s' "$v"
}

WANT_REF='${{ github.event.pull_request.head.sha }}'
CI="$(cd "$(dirname "$SCRIPT")/.." && pwd)/.github/workflows/ci.yml"
if [ ! -f "$CI" ]; then
  echo "FAIL  ci.yml not found at $CI"; fail=$((fail + 1))
else
  got_ref=$(checkout_ref "$CI") || got_ref=""
  if [ -z "$got_ref" ]; then
    # No job, no checkout step, or no `ref:` key. "Verified nothing" is a failure, never
    # a silent pass -- and this is also the state the default merge-ref checkout is in.
    echo "FAIL  kb-freshness has no actions/checkout ref: -- the default merge-ref"
    echo "        checkout makes its merge base main's tip."
    fail=$((fail + 1))
  elif [ "$got_ref" = "$WANT_REF" ]; then
    echo "ok    kb-freshness checks out the PR head sha"; pass=$((pass + 1))
  else
    echo "FAIL  kb-freshness checks out '$got_ref', wanted '$WANT_REF'"
    fail=$((fail + 1))
  fi

  # --- 13b. the reader is structural, not textual --------------------------------
  # Regression for review finding M1: the first version grepped the job block as raw
  # text, so an edit that deleted the real `ref:` while leaving the string in a comment
  # kept the assertion green over a job back on the merge ref. Feed it exactly that
  # file and require a refusal.
  doctored="$TMPROOT/ci-comment-only.yml"
  sed 's|^          ref: ${{ github.event.pull_request.head.sha }}$|          # ref: ${{ github.event.pull_request.head.sha }}|' \
    "$CI" > "$doctored"
  if grep -qF 'ref: ${{ github.event.pull_request.head.sha }}' "$doctored" \
     && ! checkout_ref "$doctored" >/dev/null 2>&1; then
    echo "ok    a ref: left only in a comment does not count as pinned"; pass=$((pass + 1))
  else
    echo "FAIL  a commented-out ref: still reads as pinned -- the check is textual"
    fail=$((fail + 1))
  fi

  # --- 13c. a renamed or removed job is a refusal, not a pass --------------------
  renamed="$TMPROOT/ci-renamed.yml"
  sed 's/^  kb-freshness:/  kb-freshness-renamed:/' "$CI" > "$renamed"
  if checkout_ref "$renamed" >/dev/null 2>&1; then
    echo "FAIL  a renamed kb-freshness job still reads as pinned"; fail=$((fail + 1))
  else
    echo "ok    a renamed or removed kb-freshness job fails closed"; pass=$((pass + 1))
  fi
fi

# --- 11. outside a git repo ---------------------------------------------------
out=$(cd "$TMPROOT" && "$SCRIPT" 2>&1); got=$?
if [ "$got" -eq 2 ]; then echo "ok    aborts outside a git repo"; pass=$((pass + 1))
else echo "FAIL  aborts outside a git repo — exit $got, wanted 2"; fail=$((fail + 1)); fi

echo ""
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
