#!/usr/bin/env bash
# prove_test.sh — prove a fix commit's regression test actually catches the bug.
#
# Runs the test files changed in the fix commit twice:
#   step 1: source rolled back to the parent commit (bug present)  -> tests MUST fail
#   step 2: source restored to the fix commit        (bug fixed)   -> tests MUST pass
# A test that passes in step 1 proves nothing and is REJECTED.
#
# Usage: scripts/prove_test.sh [FIX_REF]   (default HEAD)
#
# Runner scope: pytest under services/<svc>/ only. Non-Python tests never enter the set
# (the diff below filters to '*.py'), so a frontend-only fix aborts as "changes no test
# file". A .py test OUTSIDE services/<svc>/ — rag_eval/tests/ today — used to be skipped
# with a warning while the run still printed PROVEN. It is now REFUSED: a run that skips
# a test and reports a pass it never executed is worse than no run at all.
#
# Assumes the working tree is checked out at FIX_REF (normally HEAD, the fix you
# just committed). Only touches the tracked source files that commit changed;
# test files are always left at FIX_REF. A trap restores source on any exit.
set -uo pipefail

FIX=${1:-HEAD}
PARENT="${FIX}~1"
PY=python3

cd "$(git rev-parse --show-toplevel)" || exit 2

if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ABORT: uncommitted tracked changes — commit or stash first (we rewrite tracked files)." >&2
  exit 2
fi
git rev-parse --verify --quiet "$PARENT" >/dev/null || { echo "ABORT: $PARENT does not exist." >&2; exit 2; }

# The runner leaves test files at whatever the tree holds and only rewrites source, so a
# tree checked out somewhere other than FIX runs the WRONG tests and still prints a
# verdict. Refuse instead of reporting one.
if [ "$(git rev-parse HEAD)" != "$(git rev-parse "$FIX")" ]; then
  echo "ABORT: working tree is at $(git rev-parse --short HEAD), not $FIX. Check out $FIX (or a worktree at it) first — otherwise the tests run are not the ones being proven." >&2
  exit 2
fi

# Classify the .py files the fix commit changed: tests vs source (added/modified/deleted).
src_mods=(); src_adds=(); src_dels=(); tests=()
while IFS=$'\t' read -r status path; do
  [ -z "${path:-}" ] && continue
  case "$path" in
    *test_*.py|*_test.py|*/tests/*) tests+=("$path"); continue ;;
  esac
  case "$status" in
    A*) src_adds+=("$path") ;;
    D*) src_dels+=("$path") ;;
    *)  src_mods+=("$path") ;;   # M / R / C -> treat as modified
  esac
done < <(git diff --name-status "$PARENT" "$FIX" -- '*.py')

if [ ${#tests[@]} -eq 0 ]; then
  echo "ABORT: $FIX changes no test file — nothing to prove." >&2
  exit 2
fi

# Refuse a commit this runner cannot fully execute. Skipping a test file and still
# printing PROVEN would make the gate report a pass it never verified.
for t in "${tests[@]}"; do
  case "$t" in
    services/*/*) ;;
    *) echo "ABORT: $t is not under services/<svc>/ — this runner is pytest-only and cannot execute it. Prove it by hand or extend the runner." >&2
       exit 2 ;;
  esac
done

run_tests() {   # 0 if every changed test file passes, 1 if any fails/errors
  local overall=0 t svc rel
  for t in "${tests[@]}"; do
    case "$t" in
      services/*/*)
        svc="services/$(printf '%s' "$t" | cut -d/ -f2)"
        rel="${t#"$svc"/}"
        ( cd "$svc" && $PY -m pytest "$rel" -q ) || overall=1
        ;;
      *) echo "ERROR: $t not under services/<svc>/ — cannot run; failing closed." >&2; overall=1 ;;
    esac
  done
  return $overall
}

roll_back() {   # FIX source -> PARENT source (reintroduce the bug)
  [ ${#src_mods[@]} -gt 0 ] && git checkout "$PARENT" -- "${src_mods[@]}"
  [ ${#src_dels[@]} -gt 0 ] && git checkout "$PARENT" -- "${src_dels[@]}"
  [ ${#src_adds[@]} -gt 0 ] && rm -f "${src_adds[@]}"
  return 0
}
restore() {     # PARENT source -> FIX source (reapply the fix)
  [ ${#src_mods[@]} -gt 0 ] && git checkout "$FIX" -- "${src_mods[@]}"
  [ ${#src_dels[@]} -gt 0 ] && git checkout "$FIX" -- "${src_dels[@]}"
  [ ${#src_adds[@]} -gt 0 ] && git checkout "$FIX" -- "${src_adds[@]}"
  return 0
}
trap restore EXIT   # never leave the tree on parent source

n_src=$(( ${#src_mods[@]} + ${#src_adds[@]} + ${#src_dels[@]} ))
echo "== prove-the-test: $FIX =="
echo "tests:  ${tests[*]}"
echo "source: $n_src changed file(s)"

fail_step=skip
if [ "$n_src" -gt 0 ]; then
  echo; echo "-- step 1: bug present (source at $PARENT) — expect FAILURE --"
  roll_back
  if run_tests; then fail_step=BAD; else fail_step=GOOD; fi
  restore
else
  echo; echo "-- step 1: skipped ($FIX changes no source, only tests) --"
fi

echo; echo "-- step 2: bug fixed (source at $FIX) — expect PASS --"
if run_tests; then pass_step=GOOD; else pass_step=BAD; fi

echo; echo "== verdict =="
verdict=0
case "$fail_step" in
  BAD)  echo "FAIL: test(s) pass even without the fix — not a real regression test."; verdict=1 ;;
  GOOD) echo "OK:   test(s) fail on $PARENT (bug reproduced)." ;;
  skip) echo "NOTE: fail-step skipped — no source change, cannot prove the test catches the bug." ;;
esac
if [ "$pass_step" = BAD ]; then
  echo "FAIL: test(s) fail with the fix applied."; verdict=1
else
  echo "OK:   test(s) pass on $FIX (fix works)."
fi

[ $verdict -eq 0 ] && echo "PROVEN" || echo "REJECTED"
exit $verdict
