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
# Usage: scripts/spec_diff_gate.sh [MAP_FILE]   (defaults to scripts/spec_gate_map.txt)
# Exit 0 = every mapped line's code path and required spec/ADR both exist.
# Exit 1 = one or more required paths (code or spec/ADR) are missing.
# Exit 2 = usage / map file not found.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" 2>/dev/null || { echo "ABORT: not in a git repo." >&2; exit 2; }

MAP_FILE="${1:-scripts/spec_gate_map.txt}"
if [ ! -f "$MAP_FILE" ]; then
  echo "ABORT: map file not found: $MAP_FILE" >&2
  exit 2
fi

fail=0
while IFS= read -r line || [ -n "$line" ]; do
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

if [ "$fail" -eq 0 ]; then
  echo "spec_diff_gate: all mapped code areas have their required spec/ADR."
fi
exit "$fail"
