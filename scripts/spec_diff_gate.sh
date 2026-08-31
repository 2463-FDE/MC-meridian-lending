#!/usr/bin/env bash
# spec_diff_gate.sh — assert a code area that already has a merged spec/ADR
# still has one.
#
# This is an EXISTENCE check, not a same-PR diff check: spec and
# implementation land in separate PRs/weeks here on purpose (see
# docs/kb.md's weekly cadence). A line in scripts/spec_gate_map.txt means "this
# code path AND this spec/ADR path must both exist" — it does not require
# either side be touched in the current diff.
#
# Only code areas whose pairing has already merged belong in the map (see the
# comment at its top), so BOTH sides of every line are required to resolve —
# a code_glob that matches nothing is not "not yet built", it is a typo'd or
# stale entry (e.g. `disclosure-service/app` when the real path is
# `services/disclosure-service/app`) and must fail loud, the same as a missing
# spec. Remove the line instead of leaving it to silently no-op.
#
# The map is also audited for COVERAGE, because a gate that checks only what
# someone remembered to list reports "clean" over an unguarded control: the
# merged Week 1 LLM-client and per-service redactor pairings were absent from
# the map, so deleting them kept this gate green. Every tracked `adr/*.md` and
# `docs/specs/*.md` must therefore be either on the right-hand side of a map
# line or carry an `# EXEMPT: <doc> — <reason>` line in the map. A stale
# exemption (doc no longer tracked) or an exemption for a doc that is also
# mapped fails too, so retiring or implementing a doc forces the map to change
# with it.
#
# Usage: scripts/spec_diff_gate.sh [MAP_FILE]   (defaults to scripts/spec_gate_map.txt)
# Exit 0 = every mapped line's code path and required spec/ADR both exist, and
#          every spec/ADR is mapped or explicitly exempt.
# Exit 1 = a required path (code or spec/ADR) is missing, or a spec/ADR is
#          neither mapped nor exempt, or an exemption is malformed/stale.
# Exit 2 = ABORT — the gate could not run its check: usage / map file not found,
#          or the coverage audit's input could not be built. "Could not check"
#          is never reported as 0; see the audit's comment.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" 2>/dev/null || { echo "ABORT: not in a git repo." >&2; exit 2; }

MAP_FILE="${1:-scripts/spec_gate_map.txt}"
if [ ! -f "$MAP_FILE" ]; then
  echo "ABORT: map file not found: $MAP_FILE" >&2
  exit 2
fi

fail=0
mapped=""   # newline-delimited spec/ADR paths required by some map line
exempt=""   # newline-delimited spec/ADR paths declared to have no code path

while IFS= read -r line || [ -n "$line" ]; do
  # An exemption is read BEFORE comments are stripped: it is a comment to the
  # pairing loop but data to the coverage audit below. The token is anchored to
  # a line-leading '# EXEMPT:' with exactly one '# ' prefix, so the map's own
  # header prose can describe the syntax (indented as an example, or inside
  # backticks) without the description itself parsing as an exemption. A
  # near-miss spelling ('#EXEMPT:', '#  EXEMPT:') is not recognized, which
  # fails closed: the doc it meant to excuse is then reported UNMAPPED.
  trimmed="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  case "$trimmed" in
    '# EXEMPT:'*)
      rest="${trimmed#*EXEMPT:}"
      rest="$(echo "$rest" | sed 's/^[[:space:]]*//')"
      exempt_doc="${rest%% *}"
      # A bare em dash or hyphen is a delimiter, not a reason.
      exempt_reason="$(echo "${rest#"$exempt_doc"}" | sed 's/^[^[:alnum:]]*//;s/[[:space:]]*$//')"
      if [ -z "$exempt_doc" ] || [ -z "$exempt_reason" ]; then
        echo "MALFORMED: exemption needs '<doc_path> — <reason>': $trimmed (scripts/spec_gate_map.txt)" >&2
        fail=1
        continue
      fi
      exempt="$exempt
$exempt_doc"
      continue
      ;;
  esac

  line="${line%%#*}"
  line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [ -z "$line" ] && continue

  case "$line" in
    *'=>'*) ;;
    *)
      echo "MALFORMED: map line has no '=>' delimiter: $line (scripts/spec_gate_map.txt)" >&2
      fail=1
      continue
      ;;
  esac

  code_glob="$(echo "$line" | sed 's/[[:space:]]*=>.*//')"
  spec_path="$(echo "$line" | sed 's/.*=>[[:space:]]*//')"

  if [ -z "$code_glob" ] || [ -z "$spec_path" ]; then
    echo "MALFORMED: map line has empty code path or spec path: $line (scripts/spec_gate_map.txt)" >&2
    fail=1
    continue
  fi

  if [ "$code_glob" = "$spec_path" ]; then
    echo "MALFORMED: map line's code path and spec path are identical: $line (scripts/spec_gate_map.txt)" >&2
    fail=1
    continue
  fi

  # Only a well-formed line grants coverage in the audit below — a malformed
  # line must not double as the reason a spec/ADR looks mapped.
  mapped="$mapped
$spec_path"

  # shellcheck disable=SC2086
  if ! compgen -G "$code_glob"/* >/dev/null 2>&1 && [ ! -e "$code_glob" ]; then
    echo "MISSING: mapped code path does not exist: $code_glob (scripts/spec_gate_map.txt — typo, or a stale entry for deleted code)" >&2
    fail=1
    continue
  fi

  if [ ! -f "$spec_path" ] || [ -L "$spec_path" ] || ! git ls-files --error-unmatch -- "$spec_path" >/dev/null 2>&1; then
    echo "MISSING: $code_glob exists but $spec_path is not a tracked regular file (scripts/spec_gate_map.txt)" >&2
    fail=1
  fi
done < "$MAP_FILE"

# --- coverage audit: no spec/ADR may sit outside the map unaccounted for -----
# The audit input is built and CHECKED before the loop runs, and iterated
# without a here-document. Both halves matter: a `while read ... <<EOF
# $(git ls-files ...) EOF` loses the listing's exit status AND needs a temp
# file, so a git failure or an unwritable TMPDIR skipped this entire audit
# while `fail` stayed 0 and the success line still printed — the gate reporting
# coverage it never checked, which is the failure mode it exists to prevent. A
# pipe would restore the status check but run the loop body in a subshell where
# `fail=1` is discarded, so iteration is a newline-IFS `for` (no subshell, no
# temp file; globbing off so a path is never expanded).
in_set() { printf '%s\n' "$2" | grep -qxF -- "$1"; }

if ! audit_docs="$(git ls-files -- 'adr/*.md' 'docs/specs/*.md')"; then
  echo "ABORT: coverage audit input failed: git ls-files could not list 'adr/*.md' 'docs/specs/*.md' — refusing to report coverage over an audit that did not run." >&2
  exit 2
fi
# An empty listing is NOT an abort: `git ls-files` exiting 0 with no output is a
# truthful "this tree has no graded doc" (the map-syntax fixtures are exactly
# that), and aborting on it would also mask a pairing failure the loop above
# already recorded by replacing its exit 1 with a 2. The case that must fail
# closed is the one above — the listing could not be produced at all. That the
# globs still match THIS repo's tree is asserted in test_spec_diff_gate.sh.
audit_ifs=$IFS
IFS='
'
set -f
for doc in $audit_docs; do
  [ -z "$doc" ] && continue
  is_mapped=0; is_exempt=0
  in_set "$doc" "$mapped" && is_mapped=1
  in_set "$doc" "$exempt" && is_exempt=1

  if [ "$is_mapped" -eq 1 ] && [ "$is_exempt" -eq 1 ]; then
    echo "CONFLICT: $doc is both mapped and exempt — drop the exemption (scripts/spec_gate_map.txt)" >&2
    fail=1
  elif [ "$is_mapped" -eq 0 ] && [ "$is_exempt" -eq 0 ]; then
    echo "UNMAPPED: $doc is neither mapped to a code path nor exempt — add a map line for the code it obligates, or an '# EXEMPT: $doc — <reason>' line (scripts/spec_gate_map.txt)" >&2
    fail=1
  fi
done

# Same iteration shape for the same reason; `$exempt` may legitimately be empty
# (a map with no exemptions), so emptiness is not an abort here.
for doc in $exempt; do
  [ -z "$doc" ] && continue
  if ! git ls-files --error-unmatch -- "$doc" >/dev/null 2>&1; then
    echo "STALE: exemption names $doc, which is not a tracked file (scripts/spec_gate_map.txt)" >&2
    fail=1
  fi
done
set +f
IFS=$audit_ifs

if [ "$fail" -eq 0 ]; then
  echo "spec_diff_gate: all mapped code areas have their required spec/ADR, and every spec/ADR is mapped or exempt."
fi
exit "$fail"
