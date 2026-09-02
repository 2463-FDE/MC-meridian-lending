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
# Two cases at the end break that fixture pattern on purpose: they assert claims
# about THIS repo's tree and map, which no throwaway fixture can make. Each says
# in place why.
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

# new_repo_with_docs -> like new_repo, plus a tracked adr/0001-process.md and a
# tracked spec.md. Used by the coverage-audit cases: the audit only grades docs
# matching `adr/*.md` / `docs/specs/*.md`, so new_repo's bare tree has nothing to
# grade and cannot exercise it.
new_repo_with_docs() {
  local d
  d=$(new_repo)
  mkdir -p "$d/adr"
  echo "adr" > "$d/adr/0001-process.md"
  echo "spec" > "$d/spec.md"
  git -C "$d" add -A
  git -C "$d" -c user.email=t@t -c user.name=t commit -qm "add adr + spec"
  printf '%s' "$d"
}

# check NAME EXPECTED_EXIT REPO MAP_CONTENT [REQUIRED_OUTPUT_SUBSTRING]
# The optional 5th argument pins WHICH failure fired: several audit cases exit 1
# for more than one reason (a reasonless exemption is also an unmapped doc), so
# the exit code alone would not prove the case under test is the one that failed.
check() {
  local name=$1 want=$2 repo=$3 map_content=$4 want_out=${5:-}
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
  if [ -n "$want_out" ] && ! printf '%s' "$out" | grep -qF -- "$want_out"; then
    echo "FAIL  $name — exit $got as wanted, but output lacks '$want_out'"
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
git -C "$repo" add -A
git -C "$repo" -c user.email=t@t -c user.name=t commit -qm "add spec.md"
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

# --- 3d. spec path is a directory, not a file, fails --------------------------
# `-e` passes for anything at the pathname. A deleted spec replaced by a
# directory of the same name must still fail, not report the pairing intact.
repo=$(new_repo)
mkdir -p "$repo/spec.md"
echo "x" > "$repo/spec.md/placeholder.txt"
git -C "$repo" add -A
git -C "$repo" -c user.email=t@t -c user.name=t commit -qm "replace spec.md with a directory"
check "spec path that is a directory fails, not passes" 1 "$repo" "services/foo-service/app => spec.md"

# --- 3e. spec path is a symlink, fails -----------------------------------------
# A symlink at the spec path is not the tracked document itself, even if it
# resolves to a real file.
repo=$(new_repo)
echo "real spec" > "$repo/real_spec.md"
ln -s real_spec.md "$repo/spec.md"
git -C "$repo" add -A
git -C "$repo" -c user.email=t@t -c user.name=t commit -qm "add symlinked spec.md"
check "spec path that is a symlink fails, not passes" 1 "$repo" "services/foo-service/app => spec.md"

# --- 4. comments and blank lines are ignored ---------------------------------
repo=$(new_repo)
echo "spec" > "$repo/spec.md"
git -C "$repo" add -A
git -C "$repo" -c user.email=t@t -c user.name=t commit -qm "add spec.md"
check "comments and blanks ignored" 0 "$repo" "# a comment

services/foo-service/app => spec.md
"

# --- 4b. final map line without a trailing newline is still checked ---------
# `while IFS= read -r line` skips the loop body for a final unterminated
# line, so a map missing its trailing newline would silently skip the last
# pairing. Write the map with printf '%s' (no trailing \n) so the last line
# is unterminated, and require a missing spec on that line to still fail.
repo=$(new_repo)
printf '%s' "services/foo-service/app => spec.md" > "$repo/map.txt"
out=$(cd "$repo" && "$SCRIPT" map.txt 2>&1)
got=$?
if [ "$got" -eq 1 ]; then
  echo "ok    final line without trailing newline is still checked"
  pass=$((pass + 1))
else
  echo "FAIL  final line without trailing newline is still checked — exit $got, wanted 1"
  printf '%s\n' "$out" | sed 's/^/        /'
  fail=$((fail + 1))
fi

# --- 6. coverage audit: a spec/ADR outside the map is not silently unguarded --
# The bug these guard: the map protected only weeks 3-5, so the already-merged
# Week 1 LLM-client and per-service redactor pairings were absent and deleting
# that code kept the gate green. A gate that checks only what someone remembered
# to list must say so, not report clean.
repo=$(new_repo_with_docs)
check "spec/ADR neither mapped nor exempt fails" 1 "$repo" \
  "services/foo-service/app => spec.md" \
  "UNMAPPED: adr/0001-process.md"

# --- 6b. an exemption with a reason satisfies coverage -----------------------
repo=$(new_repo_with_docs)
check "spec/ADR with a reasoned exemption passes" 0 "$repo" \
  "services/foo-service/app => spec.md
# EXEMPT: adr/0001-process.md — establishes the ADR process itself; obligates no source file."

# --- 6c. an exemption with no reason is malformed ----------------------------
# An unjustified exemption is how coverage erodes back to the original bug.
repo=$(new_repo_with_docs)
check "exemption without a reason fails" 1 "$repo" \
  "services/foo-service/app => spec.md
# EXEMPT: adr/0001-process.md" \
  "MALFORMED: exemption needs"

# --- 6c-ii. a bare dash is a delimiter, not a reason -------------------------
repo=$(new_repo_with_docs)
check "exemption whose reason is only a dash fails" 1 "$repo" \
  "services/foo-service/app => spec.md
# EXEMPT: adr/0001-process.md —" \
  "MALFORMED: exemption needs"

# --- 6d. mapped AND exempt is a contradiction -------------------------------
# Mirrors doc_path_lint_allow.txt's policy: an exemption that has stopped being
# necessary must fail, so implementing a doc forces the map to drop it.
repo=$(new_repo_with_docs)
check "doc both mapped and exempt fails" 1 "$repo" \
  "services/foo-service/app => adr/0001-process.md
# EXEMPT: adr/0001-process.md — no code path" \
  "CONFLICT: adr/0001-process.md"

# --- 6e. an exemption naming an untracked doc is stale ------------------------
repo=$(new_repo_with_docs)
check "exemption for an untracked doc fails as stale" 1 "$repo" \
  "services/foo-service/app => adr/0001-process.md
# EXEMPT: adr/9999-deleted.md — retired last week" \
  "STALE: exemption names adr/9999-deleted.md"

# --- 6f. header prose describing the syntax is not parsed as an exemption -----
# Regression for a real self-inflicted failure: matching `EXEMPT:` anywhere in a
# comment made the map's own documentation of the syntax parse as data, so the
# gate reported STALE for the literal placeholder `<doc_path>`. Only a
# line-leading `# EXEMPT:` counts.
repo=$(new_repo_with_docs)
check "prose mentioning the exemption syntax is not an exemption" 0 "$repo" \
  "# Exemption syntax, reason mandatory:
#     # EXEMPT: <doc_path> — <why it has no enforceable code path>
# A doc may sit on a map line or in an \`# EXEMPT:\` line.
services/foo-service/app => adr/0001-process.md"

# --- 6g. a malformed pairing line does not grant coverage --------------------
# A line with no '=>' already fails; it must not ALSO make the doc it mentions
# look mapped, or one typo would hide two problems.
repo=$(new_repo_with_docs)
check "malformed pairing line does not count as coverage" 1 "$repo" \
  "services/foo-service/app adr/0001-process.md" \
  "UNMAPPED: adr/0001-process.md"

# --- 6h. the coverage audit cannot be skipped while the gate exits 0 ---------
# The audit input used to be a here-document fed by `$(git ls-files ...)`, which
# discarded both the listing's exit status and the temp file the here-doc needs.
# Either failure skipped the whole audit, left `fail` at 0, and printed the
# success line — coverage reported over an audit that never ran. Both cases must
# ABORT (exit 2), never 0.
#
# audit_stub_dir NAME BODY -> prints a dir holding an executable `git` shim.
audit_stub_dir() {
  local d
  d=$(mktemp -d "$TMPROOT/stub.XXXXXX")
  printf '%s\n' "#!/usr/bin/env bash" "$1" 'exec '"$(command -v git)"' "$@"' > "$d/git"
  chmod +x "$d/git"
  printf '%s' "$d"
}

# The shim fails ONLY the audit listing (`ls-files -- <globs>`) and passes every
# other git call — including `rev-parse --show-toplevel` and the per-line
# `ls-files --error-unmatch` — through to the real binary, so the case isolates
# the audit input rather than breaking the script at its first git call.
repo=$(new_repo_with_docs)
stub=$(audit_stub_dir 'if [ "$1" = "ls-files" ] && [ "$2" = "--" ]; then exit 1; fi')
printf '%s\n' "services/foo-service/app => adr/0001-process.md
# EXEMPT: spec.md — not a graded doc pattern" > "$repo/map.txt"
out=$(cd "$repo" && PATH="$stub:$PATH" "$SCRIPT" map.txt 2>&1)
got=$?
if [ "$got" -eq 2 ] && printf '%s' "$out" | grep -qF "ABORT: coverage audit input failed"; then
  echo "ok    audit-listing failure aborts instead of reporting clean (exit 2)"
  pass=$((pass + 1))
else
  echo "FAIL  audit-listing failure aborts — exit $got, wanted 2 with 'ABORT: coverage audit input failed'"
  printf '%s\n' "$out" | sed 's/^/        /'
  fail=$((fail + 1))
fi

# The audit's own globs are the other way it can silently grade nothing: if
# `adr/*.md` / `docs/specs/*.md` stopped matching this repo's layout, every
# coverage case above would still pass on its fixtures while the real gate
# certified zero docs. An empty listing is deliberately NOT an abort in the
# script (the map-syntax fixtures are legitimately doc-free, and aborting would
# mask a pairing failure's exit 1), so assert non-emptiness here, against the
# real tree, where it is actually a claim about coverage.
real_docs=$(git -C "$(dirname "$SCRIPT")/.." ls-files -- 'adr/*.md' 'docs/specs/*.md')
if [ -n "$real_docs" ]; then
  echo "ok    audit globs still match this repo's tree ($(printf '%s\n' "$real_docs" | wc -l | tr -d ' ') docs graded)"
  pass=$((pass + 1))
else
  echo "FAIL  audit globs match nothing in this repo — the coverage audit grades zero docs"
  fail=$((fail + 1))
fi

# ADR 0023 obligates TWO code paths that both ship on `main`: intake.py carries
# Decision 1's last-4 hop (`app/intake.py::ssn_last4`) and purge_ssn.py carries
# Decision 2's purge mechanism. The coverage audit above grades docs, not code
# paths, so a code path can be dropped from the map — replaced by a sibling
# rather than joined by one — and every case in this file still passes while the
# gate reports clean. That is not hypothetical: the map line for intake.py was
# the ADR's only line until purge_ssn.py landed. Were purge_ssn.py (an inert,
# documented-wrong scaffold) later retired as the ADR's sole line, the ADR would
# go UNMAPPED and the cheap fix would be an `# EXEMPT: ... no code path` that is
# false while intake's hop still ships. Assert both lines against the real map.
real_root=$(cd "$(dirname "$SCRIPT")/.." && pwd)
real_map="$real_root/scripts/spec_gate_map.txt"
adr_0023="adr/0023-applicant-ssn-at-rest.md"
missing=""
for code_path in services/origination-service/app/intake.py \
                 services/origination-service/app/purge_ssn.py; do
  # Only a path that still exists is owed a line — deleting the code is a
  # legitimate way to drop it, replacing its mapping is not.
  [ -e "$real_root/$code_path" ] || continue
  if ! grep -qxF "$code_path => $adr_0023" "$real_map"; then
    missing="$missing $code_path"
  fi
done
if [ -z "$missing" ]; then
  echo "ok    ADR 0023 keeps a map line for every code path it obligates"
  pass=$((pass + 1))
else
  echo "FAIL  ADR 0023 lost its map line for:$missing — spec-diff-gate no longer pairs that code with the ADR, and the doc-side coverage audit cannot see it"
  fail=$((fail + 1))
fi

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
