#!/usr/bin/env bash
# Smoke test for the Week 2 RAG eval harness (docs/spec-rag-week2.md).
# Runs the real CLI twice against the real corpus from the repo root:
#   1. cold run embeds everything; 2nd run must re-embed nothing (D1.3)
#   2. report must contain the two required findings (D1.6, D2.2)
#   3. report must contain no raw PII value from kb_dump (Sec/Comp 5)
# The harness is offline by design — no service/docker dependency.
set -euo pipefail
cd "$(dirname "$0")/.."

rm -rf rag_eval/.cache rag_eval/eval_report.md

echo "== first run (cold cache) =="
python3 -m rag_eval.run

echo "== second run (must re-embed nothing) =="
out=$(python3 -m rag_eval.run)
echo "$out"
echo "$out" | grep -q "0 embedded this run" || { echo "FAIL: second run re-embedded"; exit 1; }

report=rag_eval/eval_report.md
grep -q "data-capture failure, not a retrieval bug" "$report" \
  || { echo "FAIL: #6012 data-gap finding missing"; exit 1; }
grep -q "REFUSED" "$report" && grep -q "applications.jsonl" "$report" \
  || { echo "FAIL: kb_dump refusal missing from report"; exit 1; }

# No raw kb_dump PII value may appear in the report or cache (values checked
# dynamically so the smoke stays valid if the fixture data changes).
python3 - <<'EOF'
import json, sys
from pathlib import Path
vals = set()
for line in Path("kb_dump/applications.jsonl").read_text().splitlines():
    if line.strip():
        rec = json.loads(line)
        vals |= {str(rec[k]) for k in ("ssn","pan","dob","ein","name","address") if rec.get(k)}
for artifact in ("rag_eval/eval_report.md", "rag_eval/.cache/embeddings.json"):
    text = Path(artifact).read_text()
    leaked = [v for v in vals if v in text]
    if leaked:
        sys.exit(f"FAIL: raw PII in {artifact}")
print("PII check: clean")
EOF

echo "SMOKE PASS"
