#!/usr/bin/env bash
# check_retired_doc_paths.sh — refuse any reference to a doc path that has been
# retired by a move, anywhere in the tracked tree.
#
# Why: docs/spec-*.md, docs/runbook.md and docs/scoping-*.md moved into
# docs/specs/, docs/runbooks/ and docs/scoping/. Every reference in the tree was
# repointed in the same change, but ~25 unmerged branches still carry the old
# paths in their own prose, and merging one re-introduces a dead reference that
# nothing else grades: doc-path-lint reads only README.md, CLAUDE.md and
# docs/kb.md, and docs-drift reads the same three.
#
# Why not extend doc-path-lint instead: it asserts a cited path EXISTS, which is
# right for orientation docs and wrong for ADRs and specs. An ADR cites the path
# of a REJECTED option (adr/0006 cites services/lib/redactor.py, which must never
# exist); a spec cites work it PROPOSES (scripts/sync_logging_config.sh) and
# migrations later renumbered. Grading those 30 docs produces 99 absences, most
# of them correct-to-be-absent, and the allowlist would fill with permanent
# entries — the exact "permanent bypass" check_doc_paths.sh's own header forbids.
# This gate asks the opposite, decidable question: not "does this path exist" but
# "is this path one we KNOW is dead". No false positives, no allowlist.
#
# Scope is the whole tracked tree, not a doc list, because the reference that
# broke the build last time was neither prose nor in a doc: it was
# `Path(...) / "docs" / "runbook.md"` in a test under a blocking gate.
#
# Exit 0 = clean. Exit 1 = a retired path is cited. Exit 2 = could not run.
set -u

# This script and its test necessarily CONTAIN the retired patterns, so they are
# excluded from their own scan. The exclusion is exactly these two files and the
# test asserts that, so it cannot be widened into a hiding place.
SELF="scripts/check_retired_doc_paths.sh"
SELF_TEST="scripts/test_check_retired_doc_paths.sh"

# REGEX|||what to write instead. Extended regex (git grep -E).
RETIRED=(
  'docs/spec-\*\.md|||docs/specs/*.md'
  'docs/spec-[A-Za-z0-9._-]+\.md|||docs/specs/<name>.md (the spec- prefix is dropped; the folder carries it)'
  'docs/runbook\.md|||docs/runbooks/operations.md'
  'docs/runbook-[A-Za-z0-9._-]+\.md|||docs/runbooks/<name>.md (the runbook- prefix is dropped)'
  'docs/scoping-[A-Za-z0-9._-]+\.md|||docs/scoping/<name>.md (the scoping- prefix is dropped)'
  '"runbook\.md"|||"runbooks" / "operations.md" — a path built by joining quoted segments'
)

# The input is captured and its status checked BEFORE anything reports a verdict:
# a git failure or an unreadable index must ABORT, never print the success line
# over a scan that did not run. An EMPTY listing is deliberately NOT an abort —
# a listing git reported success for is truthful, and a fixture repo with no
# tracked file is a legitimate clean run. That the pathspec still matches THIS
# repo's tree is asserted in the test instead, where it is a claim about coverage.
if ! scanned="$(git ls-files -- . ":!$SELF" ":!$SELF_TEST")"; then
  echo "ABORT: could not list tracked files — refusing to report clean over a scan that did not run." >&2
  exit 2
fi
if [ -z "$scanned" ]; then
  scanned_count=0
else
  scanned_count=$(printf '%s\n' "$scanned" | wc -l | tr -d ' ')
fi

total_hits=0
hit_rows=""

for entry in "${RETIRED[@]}"; do
  regex=${entry%%|||*}
  advice=${entry#*|||}

  # -I skips binary files. git grep exits 0 on a match, 1 on none, >=2 on error;
  # only the error case is an abort, so a clean tree and a broken index are never
  # confused for each other.
  out=$(git grep -nIE -- "$regex" -- . ":!$SELF" ":!$SELF_TEST" 2>/dev/null)
  status=$?
  if [ "$status" -ge 2 ]; then
    echo "ABORT: git grep failed (exit $status) on /$regex/ — scan incomplete." >&2
    exit 2
  fi
  [ "$status" -eq 1 ] && continue

  # Newline-IFS `for`, not a here-document and not a pipe: a here-document needs
  # a writable TMPDIR, and a pipe runs the body in a subshell where total_hits is
  # discarded — the scan would then find hits and still report clean. Globbing off
  # so a matched line containing `*` is never expanded.
  hit_ifs=$IFS
  IFS='
'
  set -f
  for match in $out; do
    [ -z "$match" ] && continue
    total_hits=$((total_hits + 1))
    hit_rows+="  ${match}
      -> write instead: ${advice}
"
  done
  set +f
  IFS=$hit_ifs
done

if [ "$total_hits" -gt 0 ]; then
  echo "RETIRED DOC PATH — this path no longer exists in the tree:"
  echo ""
  printf '%s' "$hit_rows"
  echo "FAIL: $total_hits reference(s) to a retired doc path across $scanned_count tracked files." >&2
  echo "Repoint them. If a doc must quote an old path to describe the move itself," >&2
  echo "rephrase it — this gate has no allowlist on purpose." >&2
  exit 1
fi

echo "OK: no retired doc path cited ($scanned_count tracked files scanned)."
