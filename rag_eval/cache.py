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
        # Values are whatever the embedder returns — sparse term→weight dicts
        # (TF-IDF) or dense float lists (Bedrock). The key includes the
        # embedder signature, so entries from different backends never collide
        # in one file.
        self._data: dict[str, object] = {}
        # Keys touched this run. save() rewrites the file with ONLY these, so
        # vectors for sources removed from the corpus or newly refused by the
        # hygiene gate do not linger on disk. Without this, every corpus change
        # refits the embedder (new signature -> new keys) and orphans the old
        # entries, accumulating token-bearing vectors from prior states forever
        # (ADR 0007 rule 5: the cache is a PII-sensitive artifact).
        self._used: set[str] = set()
        if self.path.exists():
            self._data = json.loads(self.path.read_text(encoding="utf-8"))

    @staticmethod
    def key(signature: str, text: str) -> str:
        return hashlib.sha256(f"{signature}\x00{text}".encode()).hexdigest()

    def get_or_embed(self, signature: str, text: str, embed_fn):
        k = self.key(signature, text)
        self._used.add(k)
        if k in self._data:
            self.hits += 1
            return self._data[k]
        self.misses += 1
        vec = embed_fn(text)
        self._data[k] = vec
        return vec

    def save(self) -> None:
        # Prune to this run's live keys before persisting — stale entries never
        # reach disk.
        self._data = {k: self._data[k] for k in self._used}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data), encoding="utf-8")
