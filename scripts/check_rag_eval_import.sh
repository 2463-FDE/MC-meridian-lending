#!/usr/bin/env bash
# check_rag_eval_import.sh — prove the rag_eval import seam INSIDE the built image (card G2a).
#
# `services/origination-service/tests/test_rag_eval_seam.py` pins the three file edits that
# close the seam; only this script proves the thing those edits exist for — that
# `import rag_eval` resolves in the container that actually ships. Run from the repo root or
# anywhere; needs docker. CI runs it as the blocking `rag-eval-import-gate` job.
#
# The retrieval TOOL (`search_policy` on the assistant loop) is card G2b and is not built, so
# there is no route to smoke yet. This is the seam, not the feature.
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE=${IMAGE:-meridian-origination-seam-check}

echo "== build origination-service from the repo-root context =="
docker build -f services/origination-service/Dockerfile -t "$IMAGE" .

echo "== import rag_eval inside the image =="
# A bare `import rag_eval` would pass over a package whose modules were filtered out of the
# context one by one, so exercise a real submodule round-trip: embed-free vectors through the
# index, asserting the ranking the harness relies on. Every rag_eval module is imported so a
# partial copy fails here rather than at first use in production.
docker run --rm "$IMAGE" python - <<'PY'
import importlib

for name in ("cache", "chunker", "embedder", "hygiene", "index", "metrics", "report", "run"):
    importlib.import_module(f"rag_eval.{name}")

from rag_eval.index import InMemoryIndex

idx = InMemoryIndex()
idx.add("policy#a", {"fee": 1.0})
idx.add("policy#b", {"fee": 0.5, "late": 1.0})
assert len(idx) == 2, len(idx)

hits = idx.search({"fee": 1.0}, k=2)
assert [chunk_id for chunk_id, _ in hits] == ["policy#a", "policy#b"], hits
assert hits[0][1] > hits[1][1], hits

print(f"OK: rag_eval imports and searches inside the image ({len(idx)} chunks indexed)")
PY

echo "== retrieve from the mounted corpus inside the image =="
# The seam exists to carry ADR 0018's policy retrieval, and that path resolves the corpus
# directory from __file__ — which has FEWER parents in the image (/app/app/...) than in a
# checkout. A host-only test cannot see that difference; an IndexError here shipped once
# and this step is why it did not ship twice. Mount the corpus the way compose does.
docker run --rm \
  -e POLICY_RETRIEVAL_MIN_SCORE=0.05 \
  -v "$PWD/policies:/app/policies:ro" \
  "$IMAGE" python - <<'PY'
from app import assistant, policy_retrieval

answer = policy_retrieval.search("minimum age eligibility")
assert answer.is_hit, f"expected a corpus hit, got {answer.status}/{answer.reason}"
assert answer.text.strip(), "hit carried no corpus text"
assert set(answer.tool_result()) == {"status", "score"}, answer.tool_result()

refused = assistant._search_policy("late fee", "decision")
assert refused.reason == policy_retrieval.DECISION_TASK, refused

print(
    f"OK: retrieved {answer.chunk_id} at {answer.score:.4f}; "
    "decision task refused; model-visible keys are status+score only"
)
PY

echo "PASS: the rag_eval import seam holds in the built image."
