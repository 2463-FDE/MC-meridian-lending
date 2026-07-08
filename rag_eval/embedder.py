"""Pure-Python TF-IDF embedder behind a minimal interface (ledger DL-1).

Zero third-party deps, zero API calls, deterministic. Vectors are sparse
term→weight dicts, L2-normalized, so cosine similarity is a dot product.
A dense backend (sentence-transformers / API embeddings) can implement the
same fit/embed/signature contract later without touching callers.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

_TOKEN = re.compile(r"[a-z0-9]+")

_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "our", "per", "that", "the", "to", "we",
    "what", "when", "which", "with",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    if len(b) < len(a):
        a, b = b, a
    return sum(w * b[t] for t, w in a.items() if t in b)


class TfidfEmbedder:
    """fit() on the corpus, then embed() any text with the fitted idf."""

    VERSION = "tfidf-v1"

    def __init__(self) -> None:
        self._idf: dict[str, float] = {}
        self.signature: str = ""

    def fit(self, corpus_texts: list[str]) -> None:
        n = len(corpus_texts)
        df: dict[str, int] = {}
        for text in corpus_texts:
            for term in set(tokenize(text)):
                df[term] = df.get(term, 0) + 1
        self._idf = {t: math.log((n + 1) / (c + 1)) + 1.0 for t, c in df.items()}
        # Signature binds cached vectors to this exact fitted state: any corpus
        # change re-fits idf and must invalidate the cache (see cache.py).
        payload = json.dumps(sorted(self._idf.items()), separators=(",", ":"))
        self.signature = f"{self.VERSION}:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"

    def embed(self, text: str) -> dict[str, float]:
        if not self.signature:
            raise RuntimeError("TfidfEmbedder.embed() called before fit()")
        tf: dict[str, int] = {}
        for term in tokenize(text):
            if term in self._idf:  # out-of-corpus terms can never match anyway
                tf[term] = tf.get(term, 0) + 1
        vec = {t: c * self._idf[t] for t, c in tf.items()}
        norm = math.sqrt(sum(w * w for w in vec.values()))
        if norm:
            vec = {t: w / norm for t, w in vec.items()}
        return vec
