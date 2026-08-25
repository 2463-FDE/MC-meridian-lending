#!/usr/bin/env bash
# check_doc_claims.sh — fail if an authoritative doc asserts a compliance or safety
# CLAIM that is false on main. Companion to check_doc_paths.sh: that gate checks a
# cited path EXISTS; this one checks a stated claim is not a known lie.
#
# Why: README.md/CLAUDE.md/docs/kb.md are the platform's authoritative prose. The seed
# README claimed "PCI-DSS compliant (cardholder data encrypted)" while
# payment-service/app/models.py stores the PAN in a plaintext column (the CVV column is
# gone — migration 0020, D13a — but a stored PAN is still non-compliant), and told
# the reader "the real .env is already in the repo" — false and unsafe (secret-scan
# blocks a tracked .env). A false compliance claim is a governance defect, not a typo, so
# it gets a mechanical gate on the same rung as redactor-drift and doc-path-lint.
#
# The list is a FIXED set of banned literals, deliberately narrow: each entry is a claim
# that is false on main today, with the evidence. This is not a semantic reviewer — it
# cannot judge whether a spec still describes reality (that needs a human). It only
# refuses a known lie. Extend the list when a new false claim is retired; loosen it only
# by deleting an entry whose claim has become TRUE (e.g. when the card path is genuinely
# tokenized), and say so in the commit.
#
# The patterns match the AFFIRMATIVE claim only. An honest negation ("not compliant with
# PCI-DSS") does not contain the banned substring "PCI-DSS compliant", so the corrected
# README passes while the seed claim fails — which is the whole point.
#
# Usage: scripts/check_doc_claims.sh [DOC ...]   (defaults to the three below)
# Local: ./scripts/check_doc_claims.sh  — same invocation CI uses, no arguments.
# Tests: ./scripts/test_check_doc_claims.sh
# Exit 0 = no banned claim present. Exit 1 = a banned claim found, or a required doc
# absent (so none of its claims were checked). Exit 2 = usage / not a git repo.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" 2>/dev/null || { echo "ABORT: not in a git repo." >&2; exit 2; }

# Fixed banned-claims list. Each entry is "REGEX|||REASON":
#   REGEX  — extended regex (grep -iE), matches a claim that is FALSE on main.
#   REASON — why it is false, with the evidence, printed on a hit.
# Keep the regex anchored to the affirmative phrasing so an honest negation passes.
BANNED=(
  'PCI[- ]DSS compliant|||affirmative PCI-DSS compliance claim, but the PAN is stored plaintext (services/payment-service/app/models.py, D13b). Say "not compliant with PCI-DSS".'
  'cardholder data( is)? encrypted|||claims card data is encrypted, but it is stored plaintext (services/payment-service/app/models.py).'
  'real \.env is already in the repo|||unsafe and false: no .env is tracked (secret-scan blocks it). Use "cp .env.example .env".'
)

# Negators that, appearing before a banned phrase on the same clause, exempt the hit.
# Applied uniformly to every entry so an honest correction ("not PCI-DSS compliant",
# "cardholder data is not encrypted") passes while the bare affirmative still fails.
NEG="(not|isn'?t|aren'?t|never|no longer)[^.]*"

DOCS=("$@")
[ ${#DOCS[@]} -eq 0 ] && DOCS=("README.md" "CLAUDE.md" "docs/kb.md")

# Docs whose ABSENCE is tolerated (SKIPPED, not a failure) — mirrors check_doc_paths.sh.
# README.md must exist; a rename/typo then fails the gate instead of passing vacuously
# with nothing checked.
OPTIONAL_DOCS=("CLAUDE.md" "docs/kb.md")

is_optional() {  # $1 doc -> 0 if its absence is tolerated
  local d
  for d in "${OPTIONAL_DOCS[@]}"; do [ "$1" = "$d" ] && return 0; done
  return 1
}

total_hits=0
hit_rows=""
skipped_docs=""
missing_required=""

printf '%-16s %8s\n' "DOC" "CLAIMS"
printf '%-16s %8s\n' "----------------" "------"
for doc in "${DOCS[@]}"; do
  if [ ! -f "$doc" ]; then
    if is_optional "$doc"; then
      printf '%-16s %8s\n' "$doc" "SKIPPED"
      skipped_docs+=" $doc"
      continue
    fi
    printf '%-16s %8s\n' "$doc" "ABSENT"
    missing_required+=" $doc"
    continue
  fi
  h=0
  for entry in "${BANNED[@]}"; do
    regex=${entry%%|||*}
    reason=${entry#*|||}
    # grep -n so the row points the reader at the exact line to fix.
    while IFS= read -r match; do
      [ -z "$match" ] && continue
      # A negator before the banned phrase flips its meaning: "not PCI-DSS compliant"
      # contains the affirmative substring but asserts the opposite, so it must pass.
      # ERE has no lookbehind, so re-test the matched LINE for negator-then-phrase and
      # drop that hit. Same clause only ([^.]* stops at a period); a truthful negation
      # is exempt while the bare affirmative ("we are PCI-DSS compliant") still fails.
      line=${match#*:}
      if printf '%s' "$line" | grep -iqE "${NEG}${regex}"; then continue; fi
      h=$((h + 1)); total_hits=$((total_hits + 1))
      hit_rows+="  $doc:$match"$'\n'"      -> $reason"$'\n'
    done < <(grep -inE "$regex" "$doc")
  done
  printf '%-16s %8d\n' "$doc" "$h"
done

# A required doc absent from the tree had NONE of its claims checked. Fail rather than
# let a SKIPPED-style pass hide a renamed/deleted/typo'd doc — the same bypass
# check_doc_paths.sh closes.
if [ -n "$missing_required" ]; then
  echo ""
  echo "MISSING REQUIRED DOC — a doc that must exist is absent from this tree:$missing_required"
  echo "FAIL: required doc absent, so none of its claims were checked." >&2
  exit 1
fi

if [ "$total_hits" -gt 0 ]; then
  echo ""
  echo "BANNED CLAIM — an authoritative doc asserts something false on main:"
  printf '%s' "$hit_rows"
  echo "FAIL: $total_hits banned claim(s). Correct the prose, or delete the banned-list" >&2
  echo "entry in scripts/check_doc_claims.sh if the claim has genuinely become true." >&2
  exit 1
fi

echo ""
if [ -n "$skipped_docs" ]; then
  echo "NOTE: not present in this tree, so NOT checked:$skipped_docs"
fi
echo "OK: no banned claim found in the authoritative docs."
