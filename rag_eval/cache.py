"""Content-hash-keyed embedding cache (spec D1.3, ADR 0007 rule 5).

Key = sha256(embedder signature + chunk text). Unchanged corpus → every lookup
hits and nothing is re-embedded; any corpus change alters the fitted signature
and cleanly invalidates. The cache stores sparse term→weight vectors; for
TF-IDF the keys are corpus tokens (a bag of words is partially reconstructible)
— which is one more reason the hygiene gate runs BEFORE any embedding:
contaminated files must never reach this file (ADR 0007 rule 2).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


class EmbeddingCache:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.hits = 0
        self.misses = 0
        self._data: dict[str, dict[str, float]] = {}
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    @staticmethod
    def key(signature: str, text: str) -> str:
        return hashlib.sha256(f"{signature}\x00{text}".encode()).hexdigest()

    def get_or_embed(self, signature: str, text: str, embed_fn) -> dict[str, float]:
        k = self.key(signature, text)
        if k in self._data:
            self.hits += 1
            return self._data[k]
        self.misses += 1
        vec = embed_fn(text)
        self._data[k] = vec
        return vec

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data), encoding="utf-8")
