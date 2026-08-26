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

TMPROOT=$(mktemp -d); trap 'rm -rf "$TMPROOT"' EXIT
pass=0; fail=0

git_c() { git -C "$1" -c user.email=t@t -c user.name=t "${@:2}"; }

new_repo() {  # a repo with one merge commit naming a PR, one ADR, one workflow
  local d; d=$(mktemp -d "$TMPROOT/repo.XXXXXX")
  git -C "$d" init -q -b main
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

# --- 9. outside a git repo ---------------------------------------------------
out=$(cd "$TMPROOT" && "$SCRIPT" 2>&1); got=$?
if [ "$got" -eq 2 ]; then echo "ok    aborts outside a git repo"; pass=$((pass + 1))
else echo "FAIL  aborts outside a git repo — exit $got, wanted 2"; fail=$((fail + 1)); fi

echo ""
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ] || exit 1
