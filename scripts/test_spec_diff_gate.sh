#!/usr/bin/env bash
# test_spec_diff_gate.sh — tests for scripts/spec_diff_gate.sh.
#
# Each case builds a throwaway git repo, writes a fixture map file, runs the
# real script inside it (the script cd's to its own `git rev-parse
# --show-toplevel`, so the fixture repo's tree and map are what it sees), and
# asserts the exit code.
#
# The load-bearing case is `code area without its required spec fails`:
# without it the gate could pass unconditionally and nothing here would
# notice.
#
# Usage: ./scripts/test_spec_diff_gate.sh      Exit 0 = all pass, 1 = a failure.
set -uo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/spec_diff_gate.sh"
[ -x "$SCRIPT" ] || { echo "ABORT: $SCRIPT not executable." >&2; exit 1; }

TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT
pass=0
fail=0

# new_repo -> prints the path to a fresh git repo with services/foo-service/app.
new_repo() {
  local d
  d=$(mktemp -d "$TMPROOT/repo.XXXXXX")
  git -C "$d" init -q
  mkdir -p "$d/services/foo-service/app"
  echo "x" > "$d/services/foo-service/app/main.py"
  git -C "$d" add -A
  git -C "$d" -c user.email=t@t -c user.name=t commit -qm init
  printf '%s' "$d"
}

# check NAME EXPECTED_EXIT REPO MAP_CONTENT
check() {
  local name=$1 want=$2 repo=$3 map_content=$4
  local out got
  printf '%s\n' "$map_content" > "$repo/map.txt"
  out=$(cd "$repo" && "$SCRIPT" map.txt 2>&1)
  got=$?
  if [ "$got" -ne "$want" ]; then
    echo "FAIL  $name — exit $got, wanted $want"
    printf '%s\n' "$out" | sed 's/^/        /'
    fail=$((fail + 1))
    return
  fi
  echo "ok    $name"
  pass=$((pass + 1))
}

# --- 1. code area with its required spec present passes ---------------------
repo=$(new_repo)
echo "spec" > "$repo/spec.md"
check "code area with spec present passes" 0 "$repo" "services/foo-service/app => spec.md"

# --- 2. code area without its required spec fails ---------------------------
repo=$(new_repo)
check "code area without required spec fails" 1 "$repo" "services/foo-service/app => spec.md"

# --- 3. a mapped code area absent from the tree fails (never silently skips)
# The map's own policy (see its header comment) is "only list a code area once
# its paired spec/ADR has actually merged" — so an entry whose code path
# resolves to nothing is either a typo (disclosure-service/app vs the real
# services/disclosure-service/app) or a stale entry for deleted code. Either
# way it must be loud, not swallowed.
repo=$(new_repo)
check "code area absent from tree fails, not skips" 1 "$repo" "services/bar-service/app => spec.md"

# --- 3b. typo'd code path (real code one level deeper) fails the same way ---
# Reproduces the actual disclosure-service bug: the map said
# `disclosure-service/app` while the real path is
# `services/disclosure-service/app`. The typo'd prefix resolves to nothing, so
# the intended spec pairing was never actually checked.
repo=$(new_repo)
mkdir -p "$repo/services/disclosure-service/app"
echo "x" > "$repo/services/disclosure-service/app/main.py"
git -C "$repo" add -A
git -C "$repo" -c user.email=t@t -c user.name=t commit -qm "add disclosure-service"
check "typo'd code path fails instead of silently skipping" 1 "$repo" "disclosure-service/app => spec.md"

# --- 3c. malformed line (bare existing code path, no =>) fails loud ---------
# Reproduces the bug this test guards: a line with no `=>` delimiter makes
# code_glob and spec_path parse to the SAME string. If that path exists, both
# the code-path check and the spec-path check pass — the gate reports clean
# while requiring no spec/ADR at all.
repo=$(new_repo)
check "malformed line without => fails instead of self-validating" 1 "$repo" "services/foo-service/app"

# --- 4. comments and blank lines are ignored ---------------------------------
repo=$(new_repo)
echo "spec" > "$repo/spec.md"
check "comments and blanks ignored" 0 "$repo" "# a comment

services/foo-service/app => spec.md
"

# --- 5. missing map file aborts with usage exit ------------------------------
repo=$(new_repo)
out=$(cd "$repo" && "$SCRIPT" nonexistent.txt 2>&1)
got=$?
if [ "$got" -eq 2 ]; then
  echo "ok    missing map file aborts (exit 2)"
  pass=$((pass + 1))
else
  echo "FAIL  missing map file aborts — exit $got, wanted 2"
  fail=$((fail + 1))
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
