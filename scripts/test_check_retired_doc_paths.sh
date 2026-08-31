#!/usr/bin/env bash
# test_check_retired_doc_paths.sh — tests for check_retired_doc_paths.sh.
#
# The gate's own failure mode is reporting clean over a scan that found nothing
# because it scanned nothing, so the cases below cover both halves: that a
# planted retired path IS caught (one case per pattern), and that the scan
# actually reaches the tree it claims to grade.
set -u

SCRIPT="$(cd "$(dirname "$0")" && pwd)/check_retired_doc_paths.sh"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
pass=0; fail=0

new_repo() {  # -> path to a fresh git repo with one tracked file
  local r; r=$(mktemp -d)
  git -C "$r" init -q
  git -C "$r" config user.email t@t; git -C "$r" config user.name t
  mkdir -p "$r/docs" "$r/scripts"
  echo "clean prose citing docs/specs/payments-week5.md" > "$r/docs/fixture.md"
  git -C "$r" add -A >/dev/null 2>&1
  printf '%s' "$r"
}

check() {  # $1 label  $2 wanted-exit  $3 repo  [$4 substring wanted in output]
  local label=$1 want=$2 repo=$3 want_str=${4:-}
  local out got
  out=$(cd "$repo" && "$SCRIPT" 2>&1); got=$?
  if [ "$got" -ne "$want" ]; then
    echo "FAIL  $label — exit $got, wanted $want"
    printf '%s\n' "$out" | sed 's/^/        /'
    fail=$((fail + 1)); return
  fi
  if [ -n "$want_str" ] && ! printf '%s' "$out" | grep -qF -- "$want_str"; then
    echo "FAIL  $label — output missing '$want_str'"
    printf '%s\n' "$out" | sed 's/^/        /'
    fail=$((fail + 1)); return
  fi
  echo "ok    $label"
  pass=$((pass + 1))
}

# --- 1. a clean tree passes --------------------------------------------------
r=$(new_repo)
check "clean tree passes" 0 "$r"

# --- 2. every retired pattern is caught --------------------------------------
# One case per entry in RETIRED. A pattern that stops matching makes the gate
# silently narrower, which is indistinguishable from a clean tree without this.
i=0
for probe in \
  'see docs/spec-payments-week5.md for terms' \
  'every tracked docs/spec-*.md must be mapped' \
  'the runbook lives at docs/runbook.md today' \
  'see docs/runbook-rag-eval-graded-pass.md' \
  'scoped in docs/scoping-payments-week5.md' \
  'RUNBOOK = base / "docs" / "runbook.md"' \
  'SPEC = base / "docs" / "spec-payments-week5.md"' \
  "RB = base / 'docs' / 'runbook-rag-eval-graded-pass.md'" \
  'SCOPE = base / "docs" / "scoping-payments-week5.md"' \
; do
  i=$((i + 1))
  r=$(new_repo)
  printf '%s\n' "$probe" > "$r/docs/probe.md"
  git -C "$r" add -A >/dev/null 2>&1
  check "retired pattern $i caught" 1 "$r" "RETIRED DOC PATH"
done

# --- 2b. the joined-segment form is caught in EITHER quote style --------------
# Python in this repo is written with both, so a family covered only for double
# quotes is covered for half the tree.
r=$(new_repo)
mkdir -p "$r/services/x/tests"
echo "RUNBOOK = base / 'docs' / 'runbook.md'" > "$r/services/x/tests/test_x.py"
git -C "$r" add -A >/dev/null 2>&1
check "single-quoted joined segment is caught" 1 "$r" "test_x.py"

# --- 2c. one probe per folder added in the second pass -----------------------
# The 29 second-pass entries are exact filenames, so a family is only covered by
# the entries actually listed. One representative per destination folder catches
# a whole group going missing.
j=0
for probe in \
  'answered in docs/client-asks-2026-08-21-final.md' \
  'see docs/cards-week8-governance.md' \
  'the sweep in docs/regulator-watch-2026-08-14.md' \
  'specified in docs/plan-freeze-agentic-week10.md' \
  'measured in docs/servicing-money-comprehension-week6.md' \
  'the loop in docs/review-roundtrip-playbook.md' \
; do
  j=$((j + 1))
  r=$(new_repo)
  printf '%s\n' "$probe" > "$r/docs/probe.md"
  git -C "$r" add -A >/dev/null 2>&1
  check "second-pass folder $j caught" 1 "$r" "RETIRED DOC PATH"
done

# --- 2d. every replacement the gate advises actually resolves ----------------
# The advice is a literal destination for all 29 second-pass entries, so a typo
# in one would send a reader to a path that does not exist -- and nothing else
# would notice, because the gate only fails on the OLD spelling. Prose advice
# (the first-pass entries, which explain a rule) is skipped by the space/<> test.
bad_advice=""
n_advice=0
while IFS= read -r advice; do
  # skip prose advice and the glob-form entry: neither is a literal path.
  case "$advice" in *" "*|*"<"*|*"*"*) continue ;; esac
  case "$advice" in docs/*) ;; *) continue ;; esac
  n_advice=$((n_advice + 1))
  [ -f "$REPO_ROOT/$advice" ] || bad_advice="$bad_advice $advice"
done <<ADVICE
$(sed -n "s/^  '[^|]*|||\(.*\)'$/\1/p" "$SCRIPT")
ADVICE
if [ -z "$bad_advice" ] && [ "$n_advice" -ge 29 ]; then
  echo "ok    every advised replacement resolves ($n_advice checked)"
  pass=$((pass + 1))
else
  echo "FAIL  advised replacement does not resolve:$bad_advice (checked $n_advice, wanted >=29)"
  fail=$((fail + 1))
fi

# --- 3. the advice names the replacement -------------------------------------
r=$(new_repo)
echo 'see docs/runbook.md' > "$r/docs/probe.md"
git -C "$r" add -A >/dev/null 2>&1
check "hit names the replacement path" 1 "$r" "docs/runbooks/operations.md"

# --- 4. a retired path in CODE is caught, not just in prose ------------------
# The reference that broke a blocking gate last time was a path built by joining
# quoted segments inside a test, which a doc-only scan never sees.
r=$(new_repo)
mkdir -p "$r/services/x/tests"
echo 'RUNBOOK = Path(__file__).parents[3] / "docs" / "runbook.md"' > "$r/services/x/tests/test_x.py"
git -C "$r" add -A >/dev/null 2>&1
check "retired path in a .py file is caught" 1 "$r" "test_x.py"

# --- 5. only TRACKED files are graded ----------------------------------------
# CI checks out tracked files only, so an untracked local scratch file citing an
# old path must not fail the run for everyone else.
r=$(new_repo)
echo 'see docs/runbook.md' > "$r/docs/untracked.md"
check "untracked file is not graded" 0 "$r"

# --- 6. every hit is counted, not just the first -----------------------------
# The counter is incremented inside the match loop; a pipe there would run the
# body in a subshell and discard it, so the gate would find hits and still exit 0.
r=$(new_repo)
printf 'docs/runbook.md\ndocs/runbook.md\ndocs/spec-payments-week5.md\n' > "$r/docs/probe.md"
git -C "$r" add -A >/dev/null 2>&1
out=$(cd "$r" && "$SCRIPT" 2>&1)
if printf '%s' "$out" | grep -qE 'FAIL: 3 reference'; then
  echo "ok    every hit counted (3)"; pass=$((pass + 1))
else
  echo "FAIL  hit count wrong — wanted 'FAIL: 3 reference'"
  printf '%s\n' "$out" | sed 's/^/        /'; fail=$((fail + 1))
fi

# --- 7. outside a git repo, ABORT (2) rather than report clean ---------------
r=$(mktemp -d)
check "non-repo aborts (exit 2)" 2 "$r" "ABORT"

# --- 8. the scan reaches THIS repo's tree ------------------------------------
# The gate treats an empty listing as a truthful clean run (a fixture repo is
# legitimately empty), so non-emptiness is asserted here, against the real tree,
# where it is actually a claim about coverage.
out=$(cd "$REPO_ROOT" && "$SCRIPT" 2>&1)
n=$(printf '%s' "$out" | sed -n 's/.*(\([0-9][0-9]*\) tracked files scanned).*/\1/p')
if [ -n "$n" ] && [ "$n" -gt 100 ]; then
  echo "ok    scan reaches this repo's tree ($n tracked files)"; pass=$((pass + 1))
else
  echo "FAIL  scan graded $n files of this repo — it is grading nothing"
  fail=$((fail + 1))
fi

# --- 9. the real tree is clean -----------------------------------------------
check "this repo cites no retired doc path" 0 "$REPO_ROOT"

# --- 10. the self-exclusion is exactly two files -----------------------------
# The gate must exclude itself and this file (both necessarily contain the
# patterns). Any THIRD exclusion would be a place to hide a retired path, so the
# exclusion set is pinned here rather than left to review.
excl=$(grep -oE '":\!\$[A-Z_]+"' "$SCRIPT" | sort -u | tr -d '"' | tr '\n' ' ')
if [ "$excl" = ':!$SELF :!$SELF_TEST ' ]; then
  echo "ok    self-exclusion is exactly SELF + SELF_TEST"; pass=$((pass + 1))
else
  echo "FAIL  exclusion set changed: [$excl] — a third exclusion can hide a retired path"
  fail=$((fail + 1))
fi
if grep -qE 'SELF="scripts/check_retired_doc_paths.sh"' "$SCRIPT" \
   && grep -qE 'SELF_TEST="scripts/test_check_retired_doc_paths.sh"' "$SCRIPT"; then
  echo "ok    excluded paths are this gate and this test"; pass=$((pass + 1))
else
  echo "FAIL  SELF/SELF_TEST no longer name this gate and its test"
  fail=$((fail + 1))
fi

echo ""
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ]
