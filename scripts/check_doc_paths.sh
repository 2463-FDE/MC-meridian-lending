#!/usr/bin/env bash
# check_doc_paths.sh — assert every repo-path cited in backticks across the
# authoritative docs actually exists in THIS branch's tree.
#
# Why: CLAUDE.md and docs/kb.md are loaded by every agent session before it
# touches code. When they describe files that live only on an unmerged branch
# (e.g. the post-#12 world's disclosure-service/app/rules.py) the session starts
# from a wrong-by-construction state. This gate makes that drift a mechanical
# failure instead of something a human notices months later.
#
# A "path candidate" is an inline `...` span that looks like a repo path:
#   - only [A-Za-z0-9._/-]   drops commands, regex, globs (`*`), placeholders
#                            (`<file>`), version strings, code identifiers
#   - contains a `/`         a path, not a bare word / root-file mention / command
#   - not a URL
# Directory paths (trailing `/`) are checked as directories. Fenced code blocks
# are skipped — they hold commands and output, not authoritative path claims.
# Globs and angle-bracket placeholders are intentionally NOT checked: they name a
# shape, not a literal file (`services/*/app/redactor.py`, `git show main:<file>`).
#
# Root-file mentions (`ARCHITECTURE.md`, `Makefile`, `.env.example`) are a deliberate
# cut: requiring a slash is what keeps bare code identifiers and prose words out, and
# a no-slash rule cannot tell `Makefile` from a sentence. That class stays unchecked.
#
# Existence is filesystem existence in the checked-out tree. Under CI that tree
# holds only tracked files, so a doc that cites an untracked local file fails CI —
# which is the point: track the file or drop the reference.
#
# A reference that is CORRECTLY absent — docs/kb.md's branch-layout section cites
# files that live on another branch on purpose — goes in scripts/doc_path_lint_allow.txt
# with a reason. An allowlisted path that STARTS resolving fails the run: the
# exemption has outlived its reason and must be deleted, so the list cannot rot into
# a permanent bypass. An allowlist entry that no doc cites is not an error (a doc may
# be absent from this tree entirely).
#
# Usage: scripts/check_doc_paths.sh [DOC ...]   (defaults to the three below)
# Local: ./scripts/check_doc_paths.sh  — same invocation CI uses, no arguments.
# Tests: ./scripts/test_check_doc_paths.sh
# Exit 0 = all resolve. Exit 1 = one or more absent, or a stale allowlist entry.
# Exit 2 = usage.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" 2>/dev/null || { echo "ABORT: not in a git repo." >&2; exit 2; }

# Tracked-file index, once. Used for the suffix match below.
tracked=$(git ls-files)

# Known-absent references, one path per line, `#` starts a comment. Read once.
ALLOW_FILE="scripts/doc_path_lint_allow.txt"
allow=""
if [ -f "$ALLOW_FILE" ]; then
  allow=$(sed 's/#.*//' "$ALLOW_FILE" | tr -d '[:blank:]' | grep -v '^$')
fi

is_allowed() {  # $1 candidate -> 0 if listed as known-absent
  [ -n "$allow" ] && printf '%s\n' "$allow" | grep -qxF "$1"
}

resolves() {  # $1 candidate -> 0 if it names a real file/dir in this tree
  local c=$1
  [ -e "$c" ] && return 0                    # exact repo-relative path (or a dir)
  # The docs cite files service-relative (`disclosure-service/app/rules.py` for
  # `services/disclosure-service/app/rules.py`). Accept a candidate that is the
  # path-suffix of a tracked file, so shorthand resolves and only references to
  # files that exist NOWHERE in the branch fail.
  printf '%s\n' "$tracked" | grep -qxF "$c" && return 0
  printf '%s\n' "$tracked" | grep -qE "(^|/)$(printf '%s' "$c" | sed 's/[.[\*^$/]/\\&/g')\$" && return 0
  return 1
}

DOCS=("$@")
[ ${#DOCS[@]} -eq 0 ] && DOCS=("README.md" "CLAUDE.md" "docs/kb.md")

extract() {  # $1 = doc file -> unique inline-backtick candidates, one per line
  awk '/^[[:space:]]*```/{f=!f; next} !f{print}' "$1" \
    | grep -oE '`[^`]+`' \
    | sed 's/`//g' \
    | sort -u
}

is_pathish() {  # $1 candidate -> 0 if it looks like a literal repo path we can check
  local c=$1
  case "$c" in *[!A-Za-z0-9._/-]*) return 1 ;; esac   # any non-path char -> skip
  case "$c" in http*) return 1 ;; esac                # URL -> skip
  case "$c" in /*) return 1 ;; esac                   # leading slash = HTTP route or
                                                      # absolute path, not a repo path
  # A git ref, not a file: the docs' branch-layout / naming sections cite branch
  # names, which share the path shape. Repo dirs never use these top-level names.
  case "$c" in
    feature/*|feat/*|fix/*|chore/*|wip/*|backup/*|demo/*|security/*|\
    refactor/*|test/*|perf/*|ci/*|hotfix/*|release/*) return 1 ;;
  esac
  case "$c" in */*) return 0 ;; *) return 1 ;; esac   # require a slash
}

total_absent=0
absent_rows=""
skipped_docs=""

printf '%-16s %7s %7s %8s\n' "DOC" "PATHS" "ABSENT" "ALLOWED"
printf '%-16s %7s %7s %8s\n' "----------------" "-----" "------" "-------"
for doc in "${DOCS[@]}"; do
  if [ ! -f "$doc" ]; then
    # Not present in this tree, so none of its claims were verified. Named in the
    # summary below so a green run is never read as "every doc was checked".
    printf '%-16s %7s %7s %8s\n' "$doc" "-" "-" "SKIPPED"
    skipped_docs+=" $doc"
    continue
  fi
  n=0; a=0; k=0
  while IFS= read -r cand; do
    [ -z "$cand" ] && continue
    is_pathish "$cand" || continue
    n=$((n + 1))
    resolves "$cand" && continue
    if is_allowed "$cand"; then
      k=$((k + 1))
      continue
    fi
    a=$((a + 1)); total_absent=$((total_absent + 1))
    absent_rows+="  $doc  ->  $cand"$'\n'
  done < <(extract "$doc")
  printf '%-16s %7d %7d %8d\n' "$doc" "$n" "$a" "$k"
done

# An exemption for a path that now resolves is a stale claim about the tree. Fail so
# it gets deleted rather than accumulating into a list nobody trusts.
stale_rows=""
while IFS= read -r entry; do
  [ -z "$entry" ] && continue
  resolves "$entry" && stale_rows+="  $entry"$'\n'
done < <(printf '%s\n' "$allow")

if [ -n "$stale_rows" ]; then
  echo ""
  echo "STALE ALLOWLIST — $ALLOW_FILE exempts a path that DOES resolve in this tree:"
  printf '%s' "$stale_rows"
  echo "FAIL: stale allowlist entry. Delete the line — the reference is no longer absent." >&2
  exit 1
fi

if [ "$total_absent" -gt 0 ]; then
  echo ""
  echo "ABSENT — doc cites a path this branch's tree does not contain:"
  printf '%s' "$absent_rows"
  echo "FAIL: $total_absent backticked path(s) do not resolve. Track the file, drop the" >&2
  echo "reference, or add it to $ALLOW_FILE with a reason if it is absent on purpose." >&2
  exit 1
fi

echo ""
if [ -n "$skipped_docs" ]; then
  echo "NOTE: not present in this tree, so NOT checked:$skipped_docs"
fi
echo "OK: every backticked repo-path resolves in this tree (or is allowlisted)."
