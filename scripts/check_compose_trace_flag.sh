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

# A single-line flow mapping (`environment: {LLM_TRACE_CONTENT: true, ...}`) puts the
# key anywhere in the line and can nest a `}` inside the interpolation's own closing
# brace, so parsing its value with a regex is ambiguous. Rather than parse it wrong,
# refuse it outright and require the block form (one `KEY: value` per line, which
# every compose file here already uses) so the checks below have a value they can
# actually reason about.
flow=$(grep -nHE 'environment:[[:space:]]*\{' "${FILES[@]}" || true)
if [ -n "$flow" ]; then
  echo "UNSAFE — environment written as a single-line flow mapping, which this"
  echo "check cannot safely parse a value out of:"
  printf '%s\n' "$flow"
  echo "FAIL: use one KEY: value per line instead." >&2
  exit 1
fi

# Safe forms: a literal false (optionally quoted) or an interpolation whose OWN
# default is false (${VAR:-false}). Anything else -- a literal true, an
# interpolation defaulting to true, or a bare ${VAR} with no default at all -- is
# unsafe, because an unset env var and a mistyped default both surface the same way
# (nothing to grep for) unless the safe form is required explicitly.
SAFE_TAIL_MAP=':[[:space:]]*"?\$\{[A-Za-z_][A-Za-z0-9_]*:-false\}"?[[:space:]]*$|:[[:space:]]*"?false"?[[:space:]]*$'
SAFE_TAIL_LIST='=[[:space:]]*"?\$\{[A-Za-z_][A-Za-z0-9_]*:-false\}"?[[:space:]]*$|=[[:space:]]*"?false"?[[:space:]]*$'

# Block-mapping form (`LLM_TRACE_CONTENT: value`) and list form
# (`- LLM_TRACE_CONTENT=value`, which compose's `environment:` also accepts --
# a map-only anchor never even sees this shape, so it isn't flagged unsafe,
# it's silently never examined). Grouped in one subshell so the two greps'
# outputs join on their own trailing newlines rather than running together.
unsafe=$(
  grep -nHE '^[[:space:]]*LLM_TRACE_CONTENT:' "${FILES[@]}" | grep -viE "$SAFE_TAIL_MAP" || true
  grep -nHE '^[[:space:]]*-[[:space:]]*LLM_TRACE_CONTENT=' "${FILES[@]}" | grep -viE "$SAFE_TAIL_LIST" || true
)

if [ -n "$unsafe" ]; then
  echo "UNSAFE — LLM_TRACE_CONTENT set to something other than false or an"
  echo "interpolation defaulting to false. That exports prompts and raw"
  echo "provider responses to LangSmith for everyone who runs this file:"
  printf '%s\n' "$unsafe"
  echo "FAIL: export LLM_TRACE_CONTENT in your own shell for a debugging session instead." >&2
  exit 1
fi

echo "OK: every LLM_TRACE_CONTENT assignment is false or defaults to false."
