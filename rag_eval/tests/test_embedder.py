"""Unit tests for TF-IDF embedder + embedding cache (spec D1.2–D1.3)."""

import math

import pytest

from rag_eval.cache import EmbeddingCache
from rag_eval.embedder import TfidfEmbedder, cosine, tokenize

CORPUS = [
    "Approve: model score >= 660 and DTI <= 43%",
    "Late payment fee $35 flat or 5% of past-due amount",
    "Interest accrues on outstanding principal at the note rate",
]


def _fitted():
    e = TfidfEmbedder()
    e.fit(CORPUS)
    return e


def test_tokenize_keeps_numbers_drops_stopwords():
    assert "6012" in tokenize("why was application 6012 denied")
    assert "the" not in tokenize("the fee")


def test_vectors_l2_normalized():
    e = _fitted()
    vec = e.embed(CORPUS[0])
    norm = math.sqrt(sum(w * w for w in vec.values()))
    assert abs(norm - 1.0) < 1e-9


def test_relevant_text_scores_higher():
    e = _fitted()
    q = e.embed("what is the late payment fee")
    sims = [cosine(q, e.embed(t)) for t in CORPUS]
    assert sims[1] == max(sims) and sims[1] > 0


def test_deterministic():
    a, b = _fitted(), _fitted()
    assert a.signature == b.signature
    assert a.embed(CORPUS[2]) == b.embed(CORPUS[2])


def test_signature_changes_with_corpus():
    a = _fitted()
    b = TfidfEmbedder()
    b.fit(CORPUS + ["new document about mortgages"])
    assert a.signature != b.signature


def test_embed_before_fit_raises():
    with pytest.raises(RuntimeError):
        TfidfEmbedder().embed("x")


# --- cache ---

def test_cache_second_run_all_hits(tmp_path):
    e = _fitted()
    path = tmp_path / "emb.json"

    c1 = EmbeddingCache(path)
    for t in CORPUS:
        c1.get_or_embed(e.signature, t, e.embed)
    c1.save()
    assert (c1.hits, c1.misses) == (0, 3)

    c2 = EmbeddingCache(path)
    vecs = [c2.get_or_embed(e.signature, t, e.embed) for t in CORPUS]
    assert (c2.hits, c2.misses) == (3, 0)
    assert vecs[0] == e.embed(CORPUS[0])


def test_cache_invalidated_by_signature_change(tmp_path):
    e = _fitted()
    path = tmp_path / "emb.json"
    c1 = EmbeddingCache(path)
    c1.get_or_embed(e.signature, CORPUS[0], e.embed)
    c1.save()

    e2 = TfidfEmbedder()
    e2.fit(CORPUS + ["extra doc"])
    c2 = EmbeddingCache(path)
    c2.get_or_embed(e2.signature, CORPUS[0], e2.embed)
    assert c2.misses == 1


def test_cache_file_contains_no_joined_source_text(tmp_path):
    e = _fitted()
    path = tmp_path / "emb.json"
    c = EmbeddingCache(path)
    c.get_or_embed(e.signature, CORPUS[0], e.embed)
    c.save()
    assert CORPUS[0] not in path.read_text()
