#!/usr/bin/env bash
# check_volatile_claims.sh — refuse a doc claim that is TRUE when written and FALSE
# when it lands. Third gate in the doc family, and deliberately a different KIND:
#
#   check_doc_paths.sh   — does a cited path EXIST?          (existence)
#   check_doc_claims.sh  — is this a known-false literal?    (contradiction, curated list)
#   this one            — can this sentence decay at all?    (shape, whole class)
#
# Why a third: the first two grade ~3 banned literals and path existence, against ~315
# hand-copied derived facts in docs/kb.md alone. 14 of the 25 commits that ever touched
# that file were pure stale-fixes. A curated list of lies cannot close that, because the
# author who writes the decaying sentence is the same person who would have to add it to
# the list. This gate bans the SHAPE, so no one has to notice anything.
#
# The five classes, each a whole class rather than an instance:
#   V1 self-referential merge state  "PR #12 is open", "not yet on `main`", "unpushed"
#   V2 freshness stamp               "Last synced: 2026-08-24"
#   V3 base-tip assertion            "`main` tip is `0d47601`", and the verbless "tip `0d47601`"
#   V4 spelled-out count             "All nineteen are files in adr/"
#   V5 unanchored ref                a #N or a sha on a line that does not tie it to a
#                                    merge -- i.e. present-tense state, not history
#
# V1-V4 apply to every graded doc. V5 applies to docs/kb.md ONLY, because that is the
# file whose derived facts are now generated into docs/state.md; README.md and CLAUDE.md
# cite PRs inside stable rationale prose ("PR #12 was 7,009 additions across 46 files"),
# which is history and does not decay. Widening V5 to them would delete real content to
# buy nothing.
#
# An immutable citation stays legal everywhere: "merged as #77 (`ceda4e2`)" names a merge
# commit, and a merge commit never changes. That is the distinction the gate draws --
# history passes, present-tense state does not.
#
# Escape hatch: end the line with `<!-- VOLATILE-OK: reason -->`. It is AUDITED: an
# exemption on a line that no longer violates anything FAILS, so the hatch cannot rot
# open the way an unpruned allowlist does (same rule doc_path_lint_allow.txt follows).
#
# Usage: scripts/check_volatile_claims.sh [DOC ...]   (defaults to the three below)
# Tests: ./scripts/test_check_volatile_claims.sh
# Exit 0 = clean. Exit 1 = a decaying claim, a stale exemption, or a required doc absent.
# Exit 2 = usage / not a git repo.
set -uo pipefail

# Capture the toplevel into a checked variable first. `cd "$(...)"` does NOT abort on a
# failed substitution: an empty argument makes `cd ""` a successful no-op, so the guard
# never fires and the script grades whatever directory it happened to be started in.
ROOT=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "ABORT: not in a git repo." >&2; exit 2; }
[ -n "$ROOT" ] || { echo "ABORT: not in a git repo." >&2; exit 2; }
cd "$ROOT" || { echo "ABORT: cannot enter $ROOT." >&2; exit 2; }

# Class regexes. Each is "REGEX|||REASON".
COMMON=(
  '(PR )?#[0-9]+ (is|remains) (still )?(open|unmerged)|||V1 self-referential merge state: false the moment that PR merges. Cite the merge commit and the gate that holds the control instead.'
  '(is|are|stays|remains|sits) (still )?(unpushed|unmerged)|||V1 self-referential merge state: decays on push/merge, and nothing re-reads the sentence. D5 held this shape for five weeks after its PR merged.'
  '(not|isn.?t|is not) (yet )?(on|merged (in)?to) `?(main|master)`?|||V1 self-referential merge state: a doc that lands on main describing itself as not on main is false ON merge.'
  '(lives|sits|landed|held) in an (open|unmerged) (PR|branch)|||V1 self-referential merge state: names the branch that carried the work instead of the commit that landed it.'
  'Last[ -]synced|last updated:|[Aa]s of 2[0-9]{3}-[0-9]{2}-[0-9]{2}|||V2 freshness stamp: a date that asserts the page is current is a promise no one keeps. Generated state carries its own provenance.'
  '(tip|HEAD)( (is|sits at|now))?[ :=]+`?[0-9a-f]{7,40}`?|||V3 base-tip assertion: the tip moves on every merge. docs/state.md derives it.'
  '\b(All )?(ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|twenty-one|twenty-two|twenty-three)\b [a-z]*[ ]?(are|exist|live|files|ADRs|services|gates|jobs)|||V4 spelled-out count: a count of a growing set goes stale silently. The kb said "Seventeen" while adr/ held nineteen.'
)
# V5 -- kb.md only. See the header for why it is not applied to the other two.
STRICT_DOCS=("docs/kb.md")
# A ref is legal when the same line ties it to a merge. History, not state.
ANCHOR='merged|merge commit|merges|landed|shipped|superseded|generated'

DOCS=("$@")
[ ${#DOCS[@]} -eq 0 ] && DOCS=("README.md" "CLAUDE.md" "docs/kb.md")

# Mirrors check_doc_paths.sh / check_doc_claims.sh: README.md must exist, so a rename or
# typo fails the gate instead of passing vacuously with nothing graded.
OPTIONAL_DOCS=("CLAUDE.md" "docs/kb.md")
EXEMPT_MARK='<!-- VOLATILE-OK:'

is_optional() { local d; for d in "${OPTIONAL_DOCS[@]}"; do [ "$1" = "$d" ] && return 0; done; return 1; }
is_strict()   { local d; for d in "${STRICT_DOCS[@]}";   do [ "$1" = "$d" ] && return 0; done; return 1; }

# violates DOC LINE -> prints the reason of the first class hit, or nothing.
violates() {
  local doc=$1 line=$2 entry regex reason
  for entry in "${COMMON[@]}"; do
    regex=${entry%%|||*}; reason=${entry#*|||}
    if printf '%s' "$line" | grep -qiE "$regex"; then printf '%s' "$reason"; return 0; fi
  done
  is_strict "$doc" || return 1
  # V5: a #N or a commit-shaped token with no merge anchor on the line. The sha shape
  # requires BOTH a digit and an a-f letter so a bare number (an amount, a year range)
  # and an all-letter word are not mistaken for a commit.
  if printf '%s' "$line" | grep -qE '#[0-9]+|\b[0-9a-f]{7,40}\b'; then
    if printf '%s' "$line" | grep -qE '\b[0-9a-f]{7,40}\b' \
       && ! printf '%s' "$line" | grep -qE '\b(?=[0-9a-f]{7,40}\b)' 2>/dev/null; then :; fi
    # digit+letter test, done without PCRE so the gate runs on macOS grep too.
    local has_ref=0
    printf '%s' "$line" | grep -qE '#[0-9]+' && has_ref=1
    while IFS= read -r tok; do
      [ -z "$tok" ] && continue
      printf '%s' "$tok" | grep -qE '[0-9]' && printf '%s' "$tok" | grep -qE '[a-f]' && has_ref=1
    done < <(printf '%s' "$line" | grep -oE '\b[0-9a-f]{7,40}\b')
    if [ "$has_ref" -eq 1 ] && ! printf '%s' "$line" | grep -qiE "$ANCHOR"; then
      printf '%s' "V5 unanchored ref: a PR number or commit id not tied to a merge on the same line reads as present-tense state. Move it to docs/state.md (generated), or say what it merged as."
      return 0
    fi
  fi
  return 1
}

total_hits=0; stale_exempt=0; hit_rows=""; exempt_rows=""; skipped_docs=""; missing_required=""

printf '%-16s %8s %9s\n' "DOC" "CLAIMS" "EXEMPT"
printf '%-16s %8s %9s\n' "----------------" "------" "---------"
for doc in "${DOCS[@]}"; do
  if [ ! -f "$doc" ]; then
    if is_optional "$doc"; then printf '%-16s %8s %9s\n' "$doc" "SKIPPED" "-"; skipped_docs+=" $doc"; continue; fi
    printf '%-16s %8s %9s\n' "$doc" "ABSENT" "-"; missing_required+=" $doc"; continue
  fi
  h=0; e=0; n=0
  while IFS= read -r raw; do
    n=$((n + 1))
    exempted=0
    case "$raw" in *"$EXEMPT_MARK"*) exempted=1 ;; esac
    # Grade the prose with the marker removed, so the marker's own text can never
    # be what trips a class.
    line=${raw%%"$EXEMPT_MARK"*}
    if reason=$(violates "$doc" "$line"); then
      if [ "$exempted" -eq 1 ]; then
        e=$((e + 1))
      else
        h=$((h + 1)); total_hits=$((total_hits + 1))
        hit_rows+="  $doc:$n: $line"$'\n'"      -> $reason"$'\n'
      fi
    elif [ "$exempted" -eq 1 ]; then
      # A hatch held open over prose that no longer decays. Same rule as a stale
      # doc-path allowlist entry: it must fail, or coverage erodes silently.
      stale_exempt=$((stale_exempt + 1))
      exempt_rows+="  $doc:$n: $line"$'\n'"      -> STALE EXEMPTION: nothing on this line violates a class. Delete the VOLATILE-OK marker."$'\n'
    fi
  done < "$doc"
  printf '%-16s %8d %9d\n' "$doc" "$h" "$e"
done

if [ -n "$missing_required" ]; then
  echo ""
  echo "MISSING REQUIRED DOC — absent from this tree, so none of its lines were graded:$missing_required"
  echo "FAIL: required doc absent." >&2
  exit 1
fi

if [ "$total_hits" -gt 0 ] || [ "$stale_exempt" -gt 0 ]; then
  echo ""
  [ "$total_hits" -gt 0 ] && { echo "DECAYING CLAIM — true when written, false when it lands:"; printf '%s' "$hit_rows"; }
  [ "$stale_exempt" -gt 0 ] && { echo "STALE EXEMPTION:"; printf '%s' "$exempt_rows"; }
  echo "FAIL: $total_hits decaying claim(s), $stale_exempt stale exemption(s)." >&2
  echo "Derived facts belong in docs/state.md (run \`make kb\`); durable prose belongs in docs/kb.md." >&2
  exit 1
fi

echo ""
[ -n "$skipped_docs" ] && echo "NOTE: not present in this tree, so NOT graded:$skipped_docs"
echo "OK: no decaying claim in the authoritative docs."
