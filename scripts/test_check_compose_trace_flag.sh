#!/usr/bin/env bash
# test_check_compose_trace_flag.sh — tests for scripts/check_compose_trace_flag.sh.
#
# The load-bearing case is `interpolation defaulting to true passes the old
# blocklist and must now fail`: the shipped gate accepted ANY `${...}` form
# regardless of its default, so `LLM_TRACE_CONTENT: ${LLM_TRACE_CONTENT:-true}`
# in a committed compose file passed CI while exporting prompts and raw
# provider responses to LangSmith for anyone who ran it. Without this case the
# regex could regress to the old blocklist and nothing here would notice.
#
# Usage: ./scripts/test_check_compose_trace_flag.sh   Exit 0 = all pass, 1 = a failure.
set -uo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/check_compose_trace_flag.sh"
[ -x "$SCRIPT" ] || { echo "ABORT: $SCRIPT not executable." >&2; exit 1; }

TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT
pass=0
fail=0

# new_repo -> prints the path to a fresh git repo (the script cd's to its own
# `git rev-parse --show-toplevel`, so it must run inside one).
new_repo() {
  local d
  d=$(mktemp -d "$TMPROOT/repo.XXXXXX")
  git -C "$d" init -q
  echo x > "$d/.keep"
  git -C "$d" add -A
  git -C "$d" -c user.email=t@t -c user.name=t commit -qm init
  printf '%s' "$d"
}

# check NAME EXPECTED_EXIT COMPOSE_LINE
check() {
  local name=$1 want=$2 line=$3
  local repo out got
  repo=$(new_repo)
  printf 'services:\n  x:\n    environment:\n      %s\n' "$line" > "$repo/docker-compose.yml"
  out=$(cd "$repo" && "$SCRIPT" docker-compose.yml 2>&1)
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

# --- safe forms pass ----------------------------------------------------------
check "literal false passes"                     0 'LLM_TRACE_CONTENT: false'
check "quoted literal false passes"              0 'LLM_TRACE_CONTENT: "false"'
check "interpolation defaulting to false passes" 0 'LLM_TRACE_CONTENT: ${LLM_TRACE_CONTENT:-false}'

# --- unsafe forms fail ---------------------------------------------------------
check "literal true fails"                       1 'LLM_TRACE_CONTENT: true'
# The regression this file exists to pin: the shipped blocklist matched any
# line containing '${', so this exact form passed CI silently.
check "interpolation defaulting to true fails"   1 'LLM_TRACE_CONTENT: ${LLM_TRACE_CONTENT:-true}'
check "interpolation with no default fails"      1 'LLM_TRACE_CONTENT: ${LLM_TRACE_CONTENT}'
check "interpolation defaulting to a non-bool fails" 1 'LLM_TRACE_CONTENT: ${LLM_TRACE_CONTENT:-yes}'

# --- list-form environment (`- KEY=value`) is checked, not silently skipped ---
# Regression: the map-only anchor (`^[[:space:]]*LLM_TRACE_CONTENT:`) never even
# sees this shape, so `- LLM_TRACE_CONTENT=true` passed clean through the first
# fixed version of this script -- not flagged unsafe, just never examined.
list_check() {
  local name=$1 want=$2 line=$3
  local repo out got
  repo=$(new_repo)
  printf 'services:\n  x:\n    environment:\n      %s\n' "$line" > "$repo/docker-compose.yml"
  out=$(cd "$repo" && "$SCRIPT" docker-compose.yml 2>&1)
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
list_check "list-form literal false passes"                  0 '- LLM_TRACE_CONTENT=false'
list_check "list-form interpolation defaulting to false passes" 0 '- LLM_TRACE_CONTENT=${LLM_TRACE_CONTENT:-false}'
list_check "list-form literal true fails"                    1 '- LLM_TRACE_CONTENT=true'
list_check "list-form interpolation defaulting to true fails" 1 '- LLM_TRACE_CONTENT=${LLM_TRACE_CONTENT:-true}'

# --- single-line flow mapping is refused outright, not parsed -----------------
# Regression: `environment: {LLM_TRACE_CONTENT: true}` puts the key mid-line, so
# the block-mapping anchor never sees it either. Rather than parse a form whose
# `}` collides with the interpolation's own closing brace, the gate refuses any
# flow-style environment block and asks for the block form.
repo=$(new_repo)
printf 'services:\n  x:\n    environment: {LLM_TRACE_CONTENT: true}\n' > "$repo/docker-compose.yml"
out=$(cd "$repo" && "$SCRIPT" docker-compose.yml 2>&1)
got=$?
if [ "$got" -eq 1 ] && printf '%s' "$out" | grep -qF "flow mapping"; then
  echo "ok    flow-style environment block is refused, not parsed"
  pass=$((pass + 1))
else
  echo "FAIL  flow-style environment block is refused — exit $got, wanted 1 with 'flow mapping'"
  printf '%s\n' "$out" | sed 's/^/        /'
  fail=$((fail + 1))
fi

# --- no matching key at all passes (nothing unsafe to report) -----------------
repo=$(new_repo)
printf 'services:\n  x:\n    environment:\n      OTHER_VAR: true\n' > "$repo/docker-compose.yml"
out=$(cd "$repo" && "$SCRIPT" docker-compose.yml 2>&1)
got=$?
if [ "$got" -eq 0 ]; then
  echo "ok    file with no LLM_TRACE_CONTENT key passes"
  pass=$((pass + 1))
else
  echo "FAIL  file with no LLM_TRACE_CONTENT key passes — exit $got, wanted 0"
  printf '%s\n' "$out" | sed 's/^/        /'
  fail=$((fail + 1))
fi

# --- no matching files aborts (usage), not a silent pass -----------------------
repo=$(new_repo)
out=$(cd "$repo" && "$SCRIPT" 2>&1)
got=$?
if [ "$got" -eq 2 ]; then
  echo "ok    no docker-compose*.yml files aborts (exit 2)"
  pass=$((pass + 1))
else
  echo "FAIL  no docker-compose*.yml files aborts — exit $got, wanted 2"
  printf '%s\n' "$out" | sed 's/^/        /'
  fail=$((fail + 1))
fi

# --- the real tree's committed compose files still pass today ------------------
real_root=$(cd "$(dirname "$SCRIPT")/.." && pwd)
out=$(cd "$real_root" && "$SCRIPT" 2>&1)
got=$?
if [ "$got" -eq 0 ]; then
  echo "ok    real committed compose files pass"
  pass=$((pass + 1))
else
  echo "FAIL  real committed compose files pass — exit $got, wanted 0"
  printf '%s\n' "$out" | sed 's/^/        /'
  fail=$((fail + 1))
fi

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
