"""In-memory exact cosine index (ledger DL-7, ADR 0007 rule 6).

Rebuilt each run from cached vectors — no persistent chunk store. Brute-force
exact cosine over a ~9-chunk corpus is microseconds; ANN machinery would only
approximate what exact search gives for free. Ties break on chunk_id so eval
numbers are stable run to run.
"""

from __future__ import annotations

from rag_eval.embedder import cosine


class InMemoryIndex:
    def __init__(self) -> None:
        self._entries: list[tuple[str, dict[str, float]]] = []

    def add(self, chunk_id: str, vector: dict[str, float]) -> None:
        self._entries.append((chunk_id, vector))

    def __len__(self) -> int:
        return len(self._entries)

    def search(self, query_vector: dict[str, float], k: int = 5) -> list[tuple[str, float]]:
        scored = [(cid, cosine(query_vector, vec)) for cid, vec in self._entries]
        scored.sort(key=lambda pair: (-pair[1], pair[0]))
        return scored[:k]
