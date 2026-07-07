#!/usr/bin/env bash
# Copy the canonical PII redactor to every service.
#
# redactor.py must be byte-identical across all services (each runs in its own
# container and cannot import a shared package). The gateway copy is canonical:
# edit services/gateway/app/redactor.py, then run this script. CI (redactor-drift
# job) fails if the copies diverge.
set -euo pipefail
cd "$(dirname "$0")/.."

SRC="services/gateway/app/redactor.py"
for dir in services/*/app; do
  dst="$dir/redactor.py"
  [ "$dst" = "$SRC" ] && continue
  # only sync to services that already have a redactor (all backend services do)
  [ -f "$dst" ] && cp "$SRC" "$dst" && echo "synced $dst"
done
