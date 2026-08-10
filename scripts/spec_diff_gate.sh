#!/usr/bin/env bash
# spec_diff_gate.sh — assert a code area that already has a merged spec/ADR
# still has one.
#
# This is an EXISTENCE check, not a same-PR diff check: spec and
# implementation land in separate PRs/weeks here on purpose (see
# docs/kb.md's weekly cadence). A line in scripts/spec_gate_map.txt means "if
# any file under this code path exists in the tree, this spec/ADR path must
# also exist" — it does not require either side be touched in the current
# diff.
#
# Only code areas whose pairing has already merged belong in the map (see the
# comment at its top). Adding an area before its spec/ADR merges would fail
# this gate for work already on main.
#
# Usage: scripts/spec_diff_gate.sh [MAP_FILE]   (defaults to scripts/spec_gate_map.txt)
# Exit 0 = every mapped code area's required spec/ADR exists.
# Exit 1 = one or more required paths are missing.
# Exit 2 = usage / map file not found.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" 2>/dev/null || { echo "ABORT: not in a git repo." >&2; exit 2; }

MAP_FILE="${1:-scripts/spec_gate_map.txt}"
if [ ! -f "$MAP_FILE" ]; then
  echo "ABORT: map file not found: $MAP_FILE" >&2
  exit 2
fi

fail=0
while IFS= read -r line; do
  line="${line%%#*}"
  line="$(echo "$line" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
  [ -z "$line" ] && continue

  code_glob="$(echo "$line" | sed 's/[[:space:]]*=>.*//')"
  spec_path="$(echo "$line" | sed 's/.*=>[[:space:]]*//')"

  # shellcheck disable=SC2086
  if ! compgen -G "$code_glob"/* >/dev/null 2>&1 && [ ! -e "$code_glob" ]; then
    continue  # code area doesn't exist in this tree; nothing to require
  fi

  if [ ! -e "$spec_path" ]; then
    echo "MISSING: $code_glob exists but $spec_path does not (scripts/spec_gate_map.txt)" >&2
    fail=1
  fi
done < "$MAP_FILE"

if [ "$fail" -eq 0 ]; then
  echo "spec_diff_gate: all mapped code areas have their required spec/ADR."
fi
exit "$fail"
