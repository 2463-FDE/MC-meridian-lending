#!/usr/bin/env bash
# test_check_volatile_claims.sh — tests for scripts/check_volatile_claims.sh.
#
# Each case builds a throwaway git repo, writes a fixture doc, runs the real script
# inside it and asserts the exit code plus a decisive line of output.
#
# The load-bearing cases:
#   `anchored ref passes` — without it the gate could ban every PR reference outright,
#      which would gut real history from the docs to buy nothing.
#   `unanchored ref fails in kb.md only` — pins the two-tier scope. If V5 silently
#      widened to CLAUDE.md, that file's rationale prose would start failing.
#   `stale exemption fails` — the escape hatch is the part most likely to rot open.
#
# Usage: ./scripts/test_check_volatile_claims.sh    Exit 0 = all pass, 1 = a failure.
set -uo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/check_volatile_claims.sh"
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

new_repo() {  # prints the fixture path, or nothing and returns 1 if setup failed
  local d; d=$(mktemp -d "$TMPROOT/repo.XXXXXX") || return 1
  [ -n "$d" ] || return 1
  git -C "$d" init -q -b main || return 1
  mkdir -p "$d/docs" || return 1
  echo "x" > "$d/seed" || return 1
  git -C "$d" add -A || return 1
  git -C "$d" -c user.email=t@t -c user.name=t commit -qm init || return 1
  printf '%s' "$d"
}

# check NAME EXPECTED_EXIT REPO DOC [GREP_PATTERN]
check() {
  local name=$1 want=$2 repo=$3 doc=$4 pattern=${5:-}
  # A fixture that failed to build must stop the run, not be graded. Without this the
  # empty repo path makes `cd ""` a successful no-op and the case grades the real repo.
  [ -n "$repo" ] && [ -d "$repo/.git" ] || { echo "ABORT: fixture repo missing — setup failed." >&2; exit 2; }
  local out got
  out=$(cd "$repo" && "$SCRIPT" "$doc" 2>&1); got=$?
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

# --- 1. durable prose passes ------------------------------------------------
r=$(new_repo)
echo 'The gateway is the sole trust boundary; it strips inbound trust headers.' > "$r/docs/kb.md"
check "durable prose passes" 0 "$r" docs/kb.md 'OK: no decaying claim'

# --- 2. V1 self-referential merge state -------------------------------------
r=$(new_repo)
echo 'The fix is real but PR #12 is still open, so do not cite it.' > "$r/docs/kb.md"
check "V1 open-PR claim fails" 1 "$r" docs/kb.md 'V1 self-referential'

r=$(new_repo)
echo 'That control is not yet on `main`.' > "$r/docs/kb.md"
check "V1 not-on-main claim fails" 1 "$r" docs/kb.md 'V1 self-referential'

r=$(new_repo)
echo 'The branch that untracks it is unpushed.' > "$r/docs/kb.md"
check "V1 unpushed claim fails" 1 "$r" docs/kb.md 'V1 self-referential'

# --- 3. V2 freshness stamp ---------------------------------------------------
r=$(new_repo)
echo '**Last synced:** 2026-08-24. Everything below is current.' > "$r/docs/kb.md"
check "V2 freshness stamp fails" 1 "$r" docs/kb.md 'V2 freshness stamp'

# --- 4. V3 base-tip assertion ------------------------------------------------
r=$(new_repo)
echo 'The `main` tip is `0d47601` today.' > "$r/docs/kb.md"
check "V3 tip assertion fails" 1 "$r" docs/kb.md 'V[35]'

# The bare form, with no verb between "tip" and the sha, and an anchor word later on the
# line so V5 cannot fire and mask the miss. This is the shape docs/kb.md itself carried:
# "the client's current state, tip `ec5013c` (#86)" — a tip assertion V3 read straight past.
r=$(new_repo)
echo 'The base is `main`, tip `ec5013c`, and weeks 1-10 have merged.' > "$r/docs/kb.md"
check "V3 bare tip assertion fails" 1 "$r" docs/kb.md 'V3 base-tip assertion'

# --- 5. V4 spelled-out count -------------------------------------------------
r=$(new_repo)
echo 'All nineteen are files in adr/ on the base branch.' > "$r/docs/kb.md"
check "V4 spelled count fails" 1 "$r" docs/kb.md 'V4 spelled-out count'

# --- 6. V5 scope: strict on kb.md, silent elsewhere --------------------------
# This pins the two-tier design. The SAME line must fail in kb.md and pass in
# CLAUDE.md, where PR numbers sit in stable rationale prose.
r=$(new_repo)
echo 'PR #12 was 7,009 additions across 46 files.' > "$r/docs/kb.md"
check "V5 unanchored ref fails in kb.md" 1 "$r" docs/kb.md 'V5 unanchored ref'
r2=$(new_repo)
echo 'PR #12 was 7,009 additions across 46 files.' > "$r2/CLAUDE.md"
check "V5 does not apply to CLAUDE.md" 0 "$r2" CLAUDE.md 'OK: no decaying claim'

# --- 7. an anchored citation is history and must pass ------------------------
# Without this case the gate could ban every ref outright and still look green.
r=$(new_repo)
echo 'D3 merged as #77 (`ceda4e2`), held by the blocking `atomic-apply-gate`.' > "$r/docs/kb.md"
check "anchored ref passes" 0 "$r" docs/kb.md 'OK: no decaying claim'

# --- 8. a bare number or an all-letter word is not a commit ------------------
r=$(new_repo)
echo 'The ceiling is 73400 and the balance column is decade-old code.' > "$r/docs/kb.md"
check "non-commit tokens pass" 0 "$r" docs/kb.md 'OK: no decaying claim'

# --- 9. the escape hatch, and its audit --------------------------------------
r=$(new_repo)
echo 'Artifact `5c9ed224` is immutable. <!-- VOLATILE-OK: artifact id, not a commit -->' > "$r/docs/kb.md"
check "exemption suppresses a hit" 0 "$r" docs/kb.md 'OK: no decaying claim'

r=$(new_repo)
echo 'Nothing here decays at all. <!-- VOLATILE-OK: left over from an earlier edit -->' > "$r/docs/kb.md"
check "stale exemption fails" 1 "$r" docs/kb.md 'STALE EXEMPTION'

# --- 10. a required doc that is absent graded nothing ------------------------
r=$(new_repo)
check "absent required doc fails" 1 "$r" README.md 'MISSING REQUIRED DOC'

# --- 11. an optional doc that is absent is SKIPPED, not a pass ----------------
r=$(new_repo)
check "absent optional doc skips" 0 "$r" docs/kb.md 'NOT graded'

# --- 12. outside a git repo the gate must abort, not report clean ------------
out=$(cd "$TMPROOT" && "$SCRIPT" 2>&1); got=$?
if [ "$got" -eq 2 ]; then echo "ok    aborts outside a git repo"; pass=$((pass + 1));
else echo "FAIL  aborts outside a git repo — exit $got, wanted 2"; fail=$((fail + 1)); fi

echo ""
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
