#!/usr/bin/env bash
# test_check_doc_paths.sh — tests for scripts/check_doc_paths.sh.
#
# Each case builds a throwaway git repo, writes a fixture doc, runs the real script
# inside it (the script cd's to its own `git rev-parse --show-toplevel`, so the
# fixture repo's tree and allowlist are what it sees), and asserts the exit code
# and, where it matters, a decisive line of output.
#
# The load-bearing case is `absent path fails`: without it the gate could pass
# unconditionally and nothing here would notice. Every other case exists to pin a
# specific skip rule so a later widening of the regex cannot silently start
# flagging prose.
#
# Usage: ./scripts/test_check_doc_paths.sh      Exit 0 = all pass, 1 = a failure.
set -uo pipefail

SCRIPT="$(cd "$(dirname "$0")" && pwd)/check_doc_paths.sh"
[ -x "$SCRIPT" ] || { echo "ABORT: $SCRIPT not executable." >&2; exit 1; }

TMPROOT=$(mktemp -d)
trap 'rm -rf "$TMPROOT"' EXIT
pass=0
fail=0

# new_repo -> prints the path to a fresh git repo holding one real tracked file at
# services/disclosure-service/app/rules.py (the suffix-match fixture).
new_repo() {
  local d
  d=$(mktemp -d "$TMPROOT/repo.XXXXXX")
  git -C "$d" init -q
  mkdir -p "$d/services/disclosure-service/app" "$d/scripts" "$d/docs"
  echo "x" > "$d/services/disclosure-service/app/rules.py"
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

# --- 1. a path that exists resolves -----------------------------------------
r=$(new_repo)
echo 'See `services/disclosure-service/app/rules.py` for the fee.' > "$r/docs/fixture.md"
check "existing path passes" 0 "$r" 'OK: every backticked'

# --- 2. THE load-bearing case: a fabricated path fails ----------------------
r=$(new_repo)
echo 'See `docs/does-not-exist.md` for the fee.' > "$r/docs/fixture.md"
check "absent path fails" 1 "$r" 'docs/does-not-exist\.md'

# --- 3. service-relative shorthand resolves by path suffix ------------------
r=$(new_repo)
echo 'Read via `disclosure-service/app/rules.py`, which fails closed.' > "$r/docs/fixture.md"
check "suffix shorthand resolves" 0 "$r" 'OK: every backticked'

# --- 4. every documented skip rule stays skipped ----------------------------
# All of these name absent things. If any were treated as a path, exit would be 1.
r=$(new_repo)
cat > "$r/docs/fixture.md" <<'DOC'
Glob: `services/*/app/redactor.py`
Placeholder: `git show main:<file>`
URL: `http://localhost:8000/docs`
HTTP route: `/accounts/{id}/apply-payment`
Route, no braces: `/auth/login`
Branch ref: `feature/payments-week5`
Branch ref: `chore/doc-path-lint`
Bare word: `Makefile`
Bare identifier: `board_to_servicing`
Command: `make up`
DOC
check "documented skips stay skipped" 0 "$r" 'OK: every backticked'

# --- 5. fenced code blocks are not scanned ---------------------------------
r=$(new_repo)
cat > "$r/docs/fixture.md" <<'DOC'
Prose is scanned.

```bash
cat docs/never-existed.md
`docs/also-never-existed.md`
```

Back to prose.
DOC
check "fenced code not scanned" 0 "$r" 'OK: every backticked'

# --- 6. an allowlisted absent path passes ----------------------------------
r=$(new_repo)
echo 'Spec at `docs/specs/payments-week5.md` (other branch).' > "$r/docs/fixture.md"
printf '# on another branch\ndocs/specs/payments-week5.md\n' > "$r/scripts/doc_path_lint_allow.txt"
check "allowlisted absent path passes" 0 "$r" 'OK: every backticked'

# --- 7. a stale allowlist entry fails -------------------------------------
# The exemption claims the path is absent; it resolves. That must fail, or the list
# becomes a permanent bypass nobody prunes.
r=$(new_repo)
echo 'Read `services/disclosure-service/app/rules.py`.' > "$r/docs/fixture.md"
printf 'services/disclosure-service/app/rules.py\n' > "$r/scripts/doc_path_lint_allow.txt"
check "stale allowlist entry fails" 1 "$r" 'STALE ALLOWLIST'

# --- 8. an allowlist entry no doc cites is NOT an error --------------------
# CI checks out a tree where CLAUDE.md/docs/kb.md may be absent, so entries for
# their references go unused every run. Unused must stay silent.
r=$(new_repo)
echo 'Nothing cited here.' > "$r/docs/fixture.md"
printf 'docs/specs/payments-week5.md\n' > "$r/scripts/doc_path_lint_allow.txt"
check "unused allowlist entry is not an error" 0 "$r" 'OK: every backticked'

# --- 9. an absent doc: SKIPPED if optional, FAIL if required ---------------
# The required/optional split. An OPTIONAL doc (CLAUDE.md / docs/kb.md are untracked
# on every branch, so CI checks out neither) is reported SKIPPED and passes. A
# REQUIRED doc — any explicit argument, or the tracked README.md — that is absent
# FAILS: a rename, deletion, or typo'd invocation cannot turn the gate green over a
# doc whose path claims were never checked.
r=$(new_repo)   # CLAUDE.md not written -> absent + optional
out=$(cd "$r" && "$SCRIPT" CLAUDE.md 2>&1); got=$?
if [ "$got" -eq 0 ] && printf '%s\n' "$out" | grep -qE 'NOT checked:.*CLAUDE\.md'; then
  echo "ok    absent OPTIONAL doc reported SKIPPED"; pass=$((pass + 1))
else
  echo "FAIL  absent OPTIONAL doc reported SKIPPED — exit $got, wanted 0"
  printf '%s\n' "$out" | sed 's/^/        /'; fail=$((fail + 1))
fi

r=$(new_repo)   # docs/fixture.md is a required explicit arg and does not exist
out=$(cd "$r" && "$SCRIPT" docs/fixture.md 2>&1); got=$?
if [ "$got" -eq 1 ] && printf '%s\n' "$out" | grep -qE 'MISSING REQUIRED DOC.*docs/fixture\.md'; then
  echo "ok    absent REQUIRED doc fails"; pass=$((pass + 1))
else
  echo "FAIL  absent REQUIRED doc fails — exit $got, wanted 1"
  printf '%s\n' "$out" | sed 's/^/        /'; fail=$((fail + 1))
fi

# --- 10. a directory reference resolves -----------------------------------
r=$(new_repo)
echo 'Migrations live in `db/migrations/`.' > "$r/docs/fixture.md"
mkdir -p "$r/db/migrations"
check "directory reference resolves" 0 "$r" 'OK: every backticked'

echo ""
echo "passed: $pass   failed: $fail"
[ "$fail" -eq 0 ] || exit 1
