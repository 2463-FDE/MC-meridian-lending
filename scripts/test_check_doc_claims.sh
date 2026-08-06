#!/usr/bin/env bash
# test_check_doc_claims.sh — tests for scripts/check_doc_claims.sh.
#
# Each case builds a throwaway git repo, writes a fixture doc, runs the real script
# inside it (the script cd's to its own git top-level, so the fixture repo's tree is
# what it sees) and asserts the exit code and a decisive line of output.
#
# The load-bearing case is `banned claim fails`: without it the gate could pass
# unconditionally — a typo'd regex or an emptied BANNED list — and nothing here would
# notice. The `honest negation passes` case pins the other edge: the gate must refuse
# the affirmative lie without refusing the truthful correction, or the fix it demands
# would itself fail the gate.
#
# Usage: ./scripts/test_check_doc_claims.sh      Exit 0 = all pass, 1 = a failure.
set -uo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/check_doc_claims.sh"
[ -x "$SCRIPT" ] || { echo "ABORT: $SCRIPT not executable." >&2; exit 1; }

TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT
pass=0
fail=0

new_repo() {  # prints the path to a fresh git repo
  local d
  d=$(mktemp -d "$TMPROOT/repo.XXXXXX")
  git -C "$d" init -q
  mkdir -p "$d/docs"
  echo "x" > "$d/seed"
  git -C "$d" add -A
  git -C "$d" -c user.email=t@t -c user.name=t commit -qm init
  printf '%s' "$d"
}

# check NAME EXPECTED_EXIT REPO [GREP_PATTERN]
check() {
  local name=$1 want=$2 repo=$3 pattern=${4:-}
  local out got
  out=$(cd "$repo" && "$SCRIPT" docs/fixture.md 2>&1)
  got=$?
  if [ "$got" -ne "$want" ]; then
    echo "FAIL  $name — exit $got, wanted $want"
    printf '%s\n' "$out" | sed 's/^/        /'
    fail=$((fail + 1))
    return
  fi
  if [ -n "$pattern" ] && ! printf '%s\n' "$out" | grep -qE "$pattern"; then
    echo "FAIL  $name — exit $got as wanted, but output lacks /$pattern/"
    printf '%s\n' "$out" | sed 's/^/        /'
    fail=$((fail + 1))
    return
  fi
  echo "ok    $name"
  pass=$((pass + 1))
}

# --- 1. a clean doc passes --------------------------------------------------
r=$(new_repo)
echo 'The card path is **not** compliant with PCI-DSS; PAN/CVV are plaintext.' > "$r/docs/fixture.md"
check "clean doc passes" 0 "$r" 'OK: no banned claim'

# --- 2. THE load-bearing case: the affirmative PCI claim fails --------------
r=$(new_repo)
echo 'Cardholder data is encrypted and we are PCI-DSS compliant.' > "$r/docs/fixture.md"
check "banned PCI-DSS claim fails" 1 "$r" 'BANNED CLAIM'

# --- 3. the ".env is in the repo" claim fails -------------------------------
r=$(new_repo)
echo 'the real .env is already in the repo so you can just run it' > "$r/docs/fixture.md"
check "banned .env claim fails" 1 "$r" 'BANNED CLAIM'

# --- 4. the "cardholder data encrypted" claim fails on its own --------------
r=$(new_repo)
echo 'All cardholder data encrypted at rest.' > "$r/docs/fixture.md"
check "banned encryption claim fails" 1 "$r" 'BANNED CLAIM'

# --- 5. an honest negation passes -------------------------------------------
# The corrected README says "not compliant with PCI-DSS" — the affirmative substring
# "PCI-DSS compliant" is absent, so this must pass. If it failed, the gate would
# forbid the very fix it demands.
r=$(new_repo)
echo 'The payment path is **not** compliant with PCI-DSS (tracked debt, ADR 0003).' > "$r/docs/fixture.md"
check "honest negation passes" 0 "$r" 'OK: no banned claim'

# --- 6. an absent OPTIONAL doc is SKIPPED, not a failure --------------------
r=$(new_repo)   # CLAUDE.md not written -> absent + optional
out=$(cd "$r" && "$SCRIPT" CLAUDE.md 2>&1); got=$?
if [ "$got" -eq 0 ] && printf '%s\n' "$out" | grep -qE 'NOT checked:.*CLAUDE\.md'; then
  echo "ok    absent OPTIONAL doc reported SKIPPED"; pass=$((pass + 1))
else
  echo "FAIL  absent OPTIONAL doc reported SKIPPED — exit $got, wanted 0"
  printf '%s\n' "$out" | sed 's/^/        /'; fail=$((fail + 1))
fi

# --- 7. an absent REQUIRED doc fails ----------------------------------------
# A rename/typo cannot turn the gate green over a doc whose claims were never checked.
r=$(new_repo)   # docs/fixture.md is a required explicit arg and does not exist
out=$(cd "$r" && "$SCRIPT" docs/fixture.md 2>&1); got=$?
if [ "$got" -eq 1 ] && printf '%s\n' "$out" | grep -qE 'MISSING REQUIRED DOC.*docs/fixture\.md'; then
  echo "ok    absent REQUIRED doc fails"; pass=$((pass + 1))
else
  echo "FAIL  absent REQUIRED doc fails — exit $got, wanted 1"
  printf '%s\n' "$out" | sed 's/^/        /'; fail=$((fail + 1))
fi

echo ""
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ] || exit 1
