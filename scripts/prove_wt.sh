#!/usr/bin/env bash
# prove_wt.sh — run prove_test.sh in a throwaway worktree, then always clean it up.
#
# Why: prove_test.sh rewrites tracked files in place to roll the source back to the
# parent commit, so it ABORTS on a dirty tree (scripts/prove_test.sh). The main working
# tree is chronically dirty (parallel sessions, untracked docs), so sessions hand-create a
# clean detached worktree at the fix ref and run prove there — and then leak it. Six of
# those leaked worktrees ("wt12", "prove-wt", "wt-disc", ...) had to be swept by hand.
#
# This wrapper does that same clean-worktree run as ONE self-cleaning step: it adds a
# detached worktree at the fix ref, runs prove_test.sh inside it, and removes the worktree
# on EVERY exit path (success, REJECTED, ^C, error) via a trap. Nothing to sweep later.
#
# Usage: scripts/prove_wt.sh [FIX_REF]   (default HEAD)
# The fix must be COMMITTED — a worktree checks out a ref, not your uncommitted edits.
# Exit code is prove_test.sh's own verdict (0 PROVEN, 1 REJECTED, 2 ABORT, 3 UNPROVEN),
# so `make prove` and CI read it unchanged.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || exit 2

FIX_REF=${1:-HEAD}
# Resolve to a concrete SHA now: HEAD in this tree, but a detached SHA in the worktree.
FIX_SHA=$(git rev-parse --verify --quiet "$FIX_REF^{commit}") || {
  echo "ABORT: '$FIX_REF' is not a commit — the fix must be committed first." >&2
  exit 2
}

# Drop admin records for any worktree whose directory is already gone (a previously
# leaked/killed run), so `worktree add` never trips over a stale entry. Only removes
# entries with no directory — a live sibling worktree is untouched.
git worktree prune

WT_DIR=$(mktemp -d "${TMPDIR:-/tmp}/prove-wt.XXXXXX")
cleanup() {
  # --force: prove_test.sh restores source via its own trap, but a hard kill mid-rollback
  # could leave the worktree modified; we are discarding it regardless.
  git worktree remove --force "$WT_DIR" 2>/dev/null || rm -rf "$WT_DIR"
  git worktree prune
}
trap cleanup EXIT

echo "== prove in throwaway worktree: $FIX_SHA =="
echo "worktree: $WT_DIR (removed on exit)"
git worktree add --detach "$WT_DIR" "$FIX_SHA" >/dev/null || {
  echo "ABORT: could not create worktree at $FIX_SHA." >&2
  exit 2
}

( cd "$WT_DIR" && ./scripts/prove_test.sh "$FIX_SHA" )
exit $?
