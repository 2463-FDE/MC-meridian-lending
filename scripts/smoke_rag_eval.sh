#!/usr/bin/env bash
# Smoke test for the Week 2 RAG eval harness (docs/spec-rag-week2.md).
# Runs the real CLI twice against the real corpus from the repo root:
#   1. cold run embeds everything; 2nd run must re-embed nothing (D1.3)
#   2. report carries the #6012 data gap exactly when the gold set asks for it,
#      and the kb_dump refusal (D1.6-D1.7, D2.2)
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
# The #6012 subsection is gated on the run (spec D1.7), so assert the invariant
# rather than the string: it appears exactly when the gold set carries an
# unanswerable case about that application. Grepping for it unconditionally
# couples this gate to one gold set — a reworded or renumbered case would turn
# the gate red for a reason the diff does not explain — and the predicate is
# imported from `report.py` so the gate and the report cannot disagree about
# which case it is.
python3 - <<'EOF'
import json, sys
from pathlib import Path
from rag_eval.report import asks_about_the_written_denial

gold = json.loads(Path("rag_eval/gold_queries.json").read_text())["queries"]
# An empty gold set would make `expected` False and the comparison pass over an
# audit that graded nothing; spec D1.4 requires at least ten cases.
if not gold:
    sys.exit("FAIL: gold set is empty, so this check graded nothing")
expected = any(
    asks_about_the_written_denial(q["id"], q["query"], bool(q.get("unanswerable")))
    for q in gold
)
present = "data-capture failure, not a retrieval bug" in Path(
    "rag_eval/eval_report.md"
).read_text()
if present != expected:
    sys.exit(
        f"FAIL: #6012 data-gap section present={present}, "
        f"but this gold set expects {expected}"
    )
print(f"#6012 data-gap section: present={present}, matching the gold set")
EOF
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
