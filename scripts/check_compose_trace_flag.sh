#!/usr/bin/env bash
# check_compose_trace_flag.sh — assert no committed compose file can turn on
# LLM_TRACE_CONTENT.
#
# LLM_TRACE_CONTENT=true puts the prompt (system, messages), the raw provider
# response (text) and the validated body (result) on the LangSmith spans.
# ADR 0005's boundary depends on that staying off by default, so this is a
# WHITELIST, not a blocklist: the only safe forms are a literal false
# (optionally quoted) or an interpolation whose OWN default is false
# (${VAR:-false}). Anything else — a literal true, an interpolation
# defaulting to true, or a bare ${VAR} with no default at all — is unsafe and
# fails, because an unset env var and a mistyped default both surface the
# same way (nothing to grep for) unless the check requires the safe form
# explicitly.
#
# Usage: scripts/check_compose_trace_flag.sh [FILE ...]  (defaults to
#        docker-compose*.yml in the repo root)
# Tests: ./scripts/test_check_compose_trace_flag.sh
# Exit 0 = every LLM_TRACE_CONTENT assignment is safe. Exit 1 = an unsafe one
# was found. Exit 2 = usage (no matching files).
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" 2>/dev/null || { echo "ABORT: not in a git repo." >&2; exit 2; }

FILES=("$@")
if [ ${#FILES[@]} -eq 0 ]; then
  shopt -s nullglob
  FILES=(docker-compose*.yml)
  shopt -u nullglob
fi

if [ ${#FILES[@]} -eq 0 ]; then
  echo "ABORT: no docker-compose*.yml files found." >&2
  exit 2
fi

SAFE_TAIL=':[[:space:]]*"?\$\{[A-Za-z_][A-Za-z0-9_]*:-false\}"?[[:space:]]*$|:[[:space:]]*"?false"?[[:space:]]*$'

unsafe=$(grep -nHE '^[[:space:]]*LLM_TRACE_CONTENT:' "${FILES[@]}" | grep -viE "$SAFE_TAIL" || true)

if [ -n "$unsafe" ]; then
  echo "UNSAFE — LLM_TRACE_CONTENT set to something other than false or an"
  echo "interpolation defaulting to false. That exports prompts and raw"
  echo "provider responses to LangSmith for everyone who runs this file:"
  printf '%s\n' "$unsafe"
  echo "FAIL: export LLM_TRACE_CONTENT in your own shell for a debugging session instead." >&2
  exit 1
fi

echo "OK: every LLM_TRACE_CONTENT assignment is false or defaults to false."
