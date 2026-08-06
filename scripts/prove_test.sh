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
# Exit codes — PROVEN is the only success, and only step 1 can earn it:
#   0  PROVEN    tests failed at the parent and pass at FIX_REF
#   1  REJECTED  tests passed without the fix, or fail with it
#   2  ABORT     could not run (dirty tree, no parent, no test file in the commit)
#   3  UNPROVEN  FIX_REF changes no source, so no rollback happened and nothing was proved
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

# Never let one phase import the other phase's compiled bytecode. CPython judges a cached
# .pyc fresh from source mtime-SECONDS + size, and this script rolls a file back and forward
# within one second — so two same-size revisions are indistinguishable and step 2 would
# re-import step 1's rolled-back .pyc, failing against source that is no longer there
# (a false REJECTED). Disabling writes here means the guarantee no longer depends on the
# caller setting it; run_tests also clears any pre-existing cache before each phase.
export PYTHONDONTWRITEBYTECODE=1

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
# printing PROVEN would make the gate report a pass it never verified. Service tests run
# from their own directory (imports are `app.*`, relative to the service); any other Python
# test runs from the repo root, which is what repo-level suites like scripts/tests expect.
for t in "${tests[@]}"; do
  case "$t" in
    services/*/*|*.py) ;;
    *) echo "ABORT: $t is not a Python test — this runner is pytest-only and cannot execute it. Prove it by hand or extend the runner." >&2
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
        find "$svc" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null
        ( cd "$svc" && $PY -m pytest "$rel" -q ) || overall=1
        ;;
      # Repo-level tests (scripts/tests/...) run from the repo root instead. They exist
      # because some things under test are repo-level, not service-level -- this script
      # among them. Still pytest, still fails closed if pytest itself cannot collect them.
      *.py) ( $PY -m pytest "$t" -q ) || overall=1 ;;
      *) echo "ERROR: $t is not a Python test — cannot run; failing closed." >&2; overall=1 ;;
    esac
  done
  return $overall
}

roll_back() {   # FIX source -> PARENT source (reintroduce the bug); nonzero on any failure
  [ ${#src_mods[@]} -gt 0 ] && { git checkout "$PARENT" -- "${src_mods[@]}" || return 1; }
  [ ${#src_dels[@]} -gt 0 ] && { git checkout "$PARENT" -- "${src_dels[@]}" || return 1; }
  [ ${#src_adds[@]} -gt 0 ] && { rm -f -- "${src_adds[@]}" || return 1; }
  return 0
}
restore() {     # PARENT source -> FIX source (reapply the fix); nonzero on any failure
  # A file the fix DELETED is absent at FIX, so removing it IS restoring the FIX tree.
  # `git checkout "$FIX" -- <deleted>` errors ("did not match any file") and leaves the
  # parent copy behind — step 2 would then run with the bug present and the tree exit dirty.
  # `git rm` (not plain rm) also drops the index entry roll_back's checkout staged, so the
  # tree returns fully to FIX; --ignore-unmatch makes it a safe no-op when already absent.
  [ ${#src_mods[@]} -gt 0 ] && { git checkout "$FIX" -- "${src_mods[@]}" || return 1; }
  [ ${#src_adds[@]} -gt 0 ] && { git checkout "$FIX" -- "${src_adds[@]}" || return 1; }
  [ ${#src_dels[@]} -gt 0 ] && { git rm -f --ignore-unmatch -- "${src_dels[@]}" || return 1; }
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
  roll_back || { echo "ABORT: could not roll source back to $PARENT." >&2; exit 2; }
  if run_tests; then fail_step=BAD; else fail_step=GOOD; fi
  restore || { echo "ABORT: could not restore source to $FIX after step 1." >&2; exit 2; }
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
  # PR review: this used to print NOTE and leave the verdict at zero, so a commit touching
  # only tests reported PROVEN — the one word this script exists to withhold — while no
  # rollback had been attempted at all. Nothing was disproved here, but nothing was proved
  # either, and a green step 2 alone is exactly the "test that proves nothing" the header
  # promises to reject. Its own exit status, so a caller can tell "cannot prove" apart from
  # "disproved" without parsing stdout.
  skip) echo "FAIL: fail-step skipped — $FIX changes no source, so nothing was rolled back"
        echo "      and the test(s) cannot be shown to catch anything."; verdict=3 ;;
esac
if [ "$pass_step" = BAD ]; then
  # A real disproof outranks "could not prove": report REJECTED, not UNPROVEN.
  echo "FAIL: test(s) fail with the fix applied."; verdict=1
else
  echo "OK:   test(s) pass on $FIX (fix works)."
fi

case $verdict in
  0) echo "PROVEN" ;;
  3) echo "UNPROVEN" ;;
  *) echo "REJECTED" ;;
esac
exit $verdict
