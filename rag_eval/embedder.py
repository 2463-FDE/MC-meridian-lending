"""Embedders behind a minimal fit/embed/signature interface (ledger DL-1).

Two backends implement the same contract, so callers (`run.py`, `cache.py`,
`index.py`) never know which is in use — `run.make_embedder()` picks one from
``RAG_EMBEDDER``:

- ``TfidfEmbedder`` (default): pure-Python TF-IDF. Zero third-party deps, zero
  API calls, deterministic. Vectors are sparse term→weight dicts, L2-normalized.
- ``BedrockEmbedder``: dense embeddings from Amazon Bedrock (Titan/Cohere) via
  ``boto3`` — the scaling path (Phase 1 of docs/PHASE1-BEDROCK-PGVECTOR.md).
  Vectors are dense ``list[float]``, L2-normalized.

Both normalize, so cosine similarity is a plain dot product either way;
``cosine()`` dispatches on the vector shape. The persistent index stays
in-memory in Phase 1 (ADR 0007 rule 6) regardless of backend; pgvector is
Phase 2, triggered by corpus growth, not by this swap.
"""

from __future__ import annotations

import hashlib
import json
import math
import re

_TOKEN = re.compile(r"[a-z0-9]+")

_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "our",
    "per",
    "that",
    "the",
    "to",
    "we",
    "what",
    "when",
    "which",
    "with",
}


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]


def cosine(a, b) -> float:
    """Cosine similarity for either vector shape (both are L2-normalized).

    Dense (``list[float]``) → plain dot product over aligned positions; the
    same fitted embedder produced both, so lengths match. Sparse
    (``dict[str, float]``) → dot product over shared terms, iterating the
    smaller dict. A dense and a sparse vector are never compared — one
    embedder is used for the whole run.
    """
    if isinstance(a, list):
        return sum(x * y for x, y in zip(a, b))
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
        self.signature = (
            f"{self.VERSION}:{hashlib.sha256(payload.encode()).hexdigest()[:16]}"
        )

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


class BedrockEmbedder:
    """Dense embeddings from Amazon Bedrock, same contract as TfidfEmbedder.

    ``fit()`` is a no-op that only sets the signature: dense models carry no
    corpus-derived state (no idf), so nothing to learn from the corpus. The
    signature is the model id — it namespaces the on-disk cache so switching
    models (or switching away from TF-IDF) cleanly invalidates cached vectors,
    exactly like a TF-IDF re-fit does.

    ``boto3`` is imported lazily here so the default TF-IDF path stays
    stdlib-only and keyless (CI never installs boto3, never needs AWS creds).
    Auth is AWS credentials resolved by boto3 (env/profile/role), never an API
    key literal — the same posture as the LLM client's Bedrock provider
    (services/origination-service/app/llm/config.py). A client can be injected
    for tests so no test spends a real Bedrock call.
    """

    VERSION = "bedrock-v1"

    def __init__(self, model_id: str, region: str | None = None, client=None) -> None:
        self.model_id = model_id
        self.signature: str = ""
        if client is not None:
            self._client = client
        else:
            import boto3  # lazy: only when this backend is actually selected

            self._client = boto3.client("bedrock-runtime", region_name=region)

    def fit(self, corpus_texts: list[str]) -> None:
        # No corpus-derived state; the signature binds the cache to this model
        # so a model change invalidates every cached vector (see cache.py).
        self.signature = f"{self.VERSION}:{self.model_id}"

    def embed(self, text: str) -> list[float]:
        if not self.signature:
            raise RuntimeError("BedrockEmbedder.embed() called before fit()")
        response = self._client.invoke_model(
            modelId=self.model_id,
            body=json.dumps({"inputText": text}),
        )
        payload = json.loads(response["body"].read())
        vec = payload["embedding"]
        norm = math.sqrt(sum(w * w for w in vec))
        if norm:
            vec = [w / norm for w in vec]
        return vec
