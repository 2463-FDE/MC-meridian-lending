#!/usr/bin/env bash
# gen_state.sh — generate docs/state.md, the MUTABLE half of the knowledge base.
#
# Why: docs/kb.md carried ~315 hand-copied derived facts (175 PR refs, 112 shas, 28
# dates). 14 of the 25 commits that ever touched it were pure stale-fixes — the file
# spent more effort being corrected than being written. Those facts are all copies of
# git, and a copy of a source of truth decays the moment the source moves.
#
# The split: docs/kb.md keeps DURABLE prose (orientation, the read-when map, seam
# rules) which never needs a resync; everything derivable from git lives here and is
# regenerated, never edited. check_volatile_claims.sh then enforces that kb.md holds
# no mutable state, so the two halves cannot drift back together.
#
# This reads git and the tree only — no network, no GitHub API, no token. It is
# therefore runnable in CI on a plain checkout, and its output is reproducible.
#
# Usage:
#   scripts/gen_state.sh            rewrite docs/state.md from this tree
#   scripts/gen_state.sh --check    regenerate to a temp file and diff; do not write
#   scripts/gen_state.sh --check FILE   grade FILE instead of docs/state.md (tests)
#
# Exit 0 = written, or --check found no drift.
# Exit 1 = --check found drift (or the target is absent), printed as a diff.
# Exit 2 = could not run: not a git repo, no `main` ref, git failed, unwritable TMPDIR.
#          A generator that cannot read its source must NOT print a clean result --
#          exit 2 is "verified nothing", distinct from exit 0 "verified, no drift".
set -uo pipefail

# Capture the toplevel into a checked variable first. `cd "$(...)"` does NOT abort on a
# failed substitution: an empty argument makes `cd ""` a successful no-op, so the guard
# never fires and the script grades whatever directory it happened to be started in.
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "ABORT: not in a git repo." >&2; exit 2; }
[ -n "$ROOT" ] || { echo "ABORT: not in a git repo." >&2; exit 2; }
cd "$ROOT" || { echo "ABORT: cannot enter $ROOT." >&2; exit 2; }

MODE=write
TARGET="docs/state.md"
case "${1:-}" in
  --check) MODE=check; [ -n "${2:-}" ] && TARGET=$2 ;;
  "")      : ;;
  *)       echo "usage: $0 [--check [FILE]]" >&2; exit 2 ;;
esac

# The base to describe is the MERGE BASE of this branch and main, not main's moving
# tip. Two reasons, both about the gate above this one not flapping:
#
#  1. A branch cut at commit X regenerates byte-identical output no matter what merges
#     into main while its PR is open. Describing main's tip instead would turn every
#     open PR red the moment a sibling merged -- friction that gets a gate disabled.
#  2. A merge commit cannot be predicted before it exists, so "state.md exactly equals
#     main after this merges" is unachievable by construction. What IS achievable, and
#     is what actually stops the rot, is "state.md is machine-generated from this
#     branch's base" -- that kills hand-editing and the omission-staleness that made
#     the old hand-written ledger drift 9 PRs behind. Each new branch cut absorbs
#     whatever landed since.
#
# On main itself the merge base IS the tip, so the page is exact there.
# origin/main is preferred over the local branch: a laptop whose local `main` lags
# would otherwise generate a different page than CI does.
BASE_REF=""
for ref in origin/main main; do
  if git rev-parse --verify -q "$ref" >/dev/null; then BASE_REF=$ref; break; fi
done
[ -n "$BASE_REF" ] || { echo "ABORT: neither main nor origin/main exists in this repo." >&2; exit 2; }
BASE=$(git merge-base "$BASE_REF" HEAD) || {
  echo "ABORT: no merge base between $BASE_REF and HEAD." >&2; exit 2; }
[ -n "$BASE" ] || { echo "ABORT: empty merge base for $BASE_REF." >&2; exit 2; }
BASE_LABEL="$BASE_REF ($(git rev-parse --short "$BASE"))"

TMP=$(mktemp) || { echo "ABORT: cannot create a temp file (TMPDIR unwritable?)." >&2; exit 2; }
trap 'rm -f "$TMP"' EXIT

# --- 1. base tip ------------------------------------------------------------
# Captured into a checked variable, never interpolated straight into the output:
# a git failure must abort, not emit an empty field that reads as a real answer.
tip=$(git log -1 --format='%h %ad %s' --date=short "$BASE") || {
  echo "ABORT: git log failed on $BASE." >&2; exit 2; }

# --- 2. merged-PR ledger ----------------------------------------------------
# Every merge commit on the base whose subject names a PR. Completeness is by
# construction here: the hand-written ledger in kb.md went stale by OMISSION (its
# own commit log records it "9 PRs stale before this pass"), which no truth-checker
# can catch. Generating it removes that failure mode outright.
# The git read and the filter are SEPARATE statements on purpose. As one pipeline
# under `set -o pipefail`, grep's exit 1 for "no match" is indistinguishable from git
# failing outright -- so an empty result would abort, and a `|| ledger=""` catch-all
# would swallow the real git failure instead. Split, git's status is checked and
# grep's non-match is allowed to mean nothing matched.
raw_merges=$(git log "$BASE" --merges --format='%h|%ad|%s' --date=short) || {
  echo "ABORT: git log --merges failed on $BASE." >&2; exit 2; }
ledger=$(printf '%s\n' "$raw_merges" | grep -E 'Merge pull request #[0-9]+') || ledger=""
# An EMPTY ledger is not an abort: a repo with no merge commits is truthfully empty,
# and aborting there would mask a real result. Only a git FAILURE aborts, above.

# --- 3. ADR index -----------------------------------------------------------
raw_tree=$(git ls-tree -r --name-only "$BASE" -- adr/) || {
  echo "ABORT: git ls-tree failed on $BASE:adr/." >&2; exit 2; }
# Same split as the ledger: a repo with no adr/ directory is truthfully empty.
adrs=$(printf '%s\n' "$raw_tree" | grep -E '^adr/[0-9]{4}-.*\.md$' | sort) || adrs=""

# --- 4. CI jobs, and which of them block ------------------------------------
# A job blocks unless its body contains continue-on-error or a `|| true`. CLAUDE.md's
# hand-maintained copy of this list drifted far enough to need its own resync PR.
ci=$(git show "$BASE:.github/workflows/ci.yml" 2>/dev/null) || ci=""
jobs=""
if [ -n "$ci" ]; then
  jobs=$(printf '%s\n' "$ci" | awk '
    /^jobs:/            { injobs = 1; next }
    injobs && /^[^ #]/  { injobs = 0 }
    injobs && /^  [A-Za-z0-9_-]+:[[:space:]]*$/ {
      if (name != "") print (soft ? "soft" : "BLOCKING") "|" name
      name = $1; sub(/:$/, "", name); soft = 0; next
    }
    injobs && name != "" {
      # Strip comments before testing. Several BLOCKING jobs SAY "no continue-on-error,
      # no `|| true`" in their header comment; scanning raw text marked them soft and
      # undercounted blocking jobs by more than half.
      line = $0
      sub(/^[[:space:]]*#.*$/, "", line)
      sub(/[[:space:]]#.*$/, "", line)
      if (line ~ /continue-on-error:[[:space:]]*true/ || line ~ /\|\| true/) soft = 1
    }
    END { if (name != "") print (soft ? "soft" : "BLOCKING") "|" name }
  ')
fi

# --- render -----------------------------------------------------------------
{
  echo "<!-- GENERATED by scripts/gen_state.sh — DO NOT EDIT. Run \`make kb\`. -->"
  echo "# Meridian Lending — derived state"
  echo
  echo "Every fact on this page is read out of git by \`scripts/gen_state.sh\`. Nothing here is"
  echo "hand-written, so nothing here can go stale without \`kb-freshness\` turning red."
  echo "Durable prose — orientation, the read-when map, why a seam is shaped the way it is —"
  echo "lives in \`docs/kb.md\` and is deliberately absent here."
  echo
  echo "## Base tip"
  echo
  echo "Base: \`$BASE_LABEL\` — $tip"
  echo
  echo "## Merged pull requests"
  echo
  if [ -z "$ledger" ]; then
    echo "_No merge commit on the base names a pull request._"
  else
    echo "| PR | merge | date | subject |"
    echo "|---|---|---|---|"
    while IFS='|' read -r sha date subject; do
      [ -z "$sha" ] && continue
      num=$(printf '%s' "$subject" | grep -oE '#[0-9]+' | head -1)
      echo "| $num | \`$sha\` | $date | $subject |"
    done <<< "$ledger"
  fi
  echo
  echo "## ADRs on the base"
  echo
  if [ -z "$adrs" ]; then
    echo "_No ADR file on the base._"
  else
    n=0
    while IFS= read -r f; do
      [ -z "$f" ] && continue
      n=$((n + 1))
      echo "- \`$f\`"
    done <<< "$adrs"
    echo
    echo "Count: $n. The next free number is one past the highest above."
  fi
  echo
  echo "## CI jobs"
  echo
  if [ -z "$jobs" ]; then
    echo "_No workflow read._"
  else
    echo "A job is BLOCKING unless its body carries \`continue-on-error\` or \`|| true\`."
    echo
    while IFS='|' read -r kind name; do
      [ -z "$name" ] && continue
      echo "- **$kind** — \`$name\`"
    done <<< "$jobs"
  fi
} > "$TMP"

if [ "$MODE" = write ]; then
  mkdir -p "$(dirname "$TARGET")"
  cp "$TMP" "$TARGET" || { echo "ABORT: cannot write $TARGET." >&2; exit 2; }
  echo "OK: wrote $TARGET from $BASE_LABEL."
  exit 0
fi

if [ ! -f "$TARGET" ]; then
  echo "FAIL: $TARGET does not exist. Run \`make kb\` to generate it." >&2
  exit 1
fi
if diff -u "$TARGET" "$TMP" > /dev/null; then
  echo "OK: $TARGET matches the base $BASE_LABEL."
  exit 0
fi
echo "STALE: $TARGET no longer matches git. Run \`make kb\` and commit the result."
diff -u "$TARGET" "$TMP" | sed 's/^/  /'
exit 1
