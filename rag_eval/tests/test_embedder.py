"""Unit tests for embedders + embedding cache (spec D1.2–D1.3)."""

import io
import json
import math

import pytest

from rag_eval.cache import EmbeddingCache
from rag_eval.embedder import BedrockEmbedder, TfidfEmbedder, cosine, tokenize

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


def test_cache_save_prunes_stale_entries(tmp_path):
    # A source embedded in an earlier run but absent from the current run must
    # not survive on disk — even when .cache is never deleted between runs.
    e = _fitted()
    path = tmp_path / "emb.json"

    c1 = EmbeddingCache(path)
    for t in CORPUS:
        c1.get_or_embed(e.signature, t, e.embed)
    c1.save()
    stale_key = EmbeddingCache.key(e.signature, CORPUS[0])
    assert stale_key in json.loads(path.read_text())

    # Second run touches only the other two chunks (CORPUS[0] removed/refused).
    c2 = EmbeddingCache(path)
    for t in CORPUS[1:]:
        c2.get_or_embed(e.signature, t, e.embed)
    c2.save()

    on_disk = json.loads(path.read_text())
    assert stale_key not in on_disk
    assert len(on_disk) == 2


def test_cache_file_contains_no_joined_source_text(tmp_path):
    e = _fitted()
    path = tmp_path / "emb.json"
    c = EmbeddingCache(path)
    c.get_or_embed(e.signature, CORPUS[0], e.embed)
    c.save()
    assert CORPUS[0] not in path.read_text()


# --- dense cosine (Bedrock-shaped vectors) ---


def test_dense_cosine_identical_and_orthogonal():
    a = [0.6, 0.8]  # already unit-norm
    assert abs(cosine(a, a) - 1.0) < 1e-9
    assert abs(cosine([1.0, 0.0], [0.0, 1.0])) < 1e-9


def test_dense_cosine_matches_manual_dot():
    a, b = [0.5, 0.5, 0.5, 0.5], [1.0, 0.0, 0.0, 0.0]
    assert abs(cosine(a, b) - 0.5) < 1e-9


# --- BedrockEmbedder (injected fake client — no real API call) ---


class _FakeBedrockClient:
    """Stands in for boto3 bedrock-runtime: canned raw vectors per input text.

    Returns un-normalized vectors so the test proves the embedder normalizes.
    Records call count so we can assert the cache prevents re-embedding.
    """

    def __init__(self, vectors: dict[str, list[float]]):
        self._vectors = vectors
        self.calls = 0

    def invoke_model(self, modelId, body):  # noqa: N803 — boto3's kwarg name
        self.calls += 1
        text = json.loads(body)["inputText"]
        payload = json.dumps({"embedding": self._vectors[text]})
        return {"body": io.BytesIO(payload.encode())}


def _bedrock(vectors):
    return BedrockEmbedder(model_id="fake-model", client=_FakeBedrockClient(vectors))


def test_bedrock_embed_normalizes():
    e = _bedrock({"hello world": [3.0, 4.0]})  # norm 5 -> [0.6, 0.8]
    e.fit(["hello world"])
    vec = e.embed("hello world")
    assert vec == pytest.approx([0.6, 0.8])
    assert abs(math.sqrt(sum(w * w for w in vec)) - 1.0) < 1e-9


def test_bedrock_signature_is_model_bound():
    a = _bedrock({})
    a.fit([])
    b = BedrockEmbedder(model_id="other-model", client=_FakeBedrockClient({}))
    b.fit([])
    assert a.signature == "bedrock-v1:fake-model"
    assert a.signature != b.signature


def test_bedrock_embed_before_fit_raises():
    with pytest.raises(RuntimeError):
        _bedrock({}).embed("x")


def test_bedrock_cache_prevents_re_embedding(tmp_path):
    client = _FakeBedrockClient({"doc a": [1.0, 0.0], "doc b": [0.0, 2.0]})
    e = BedrockEmbedder(model_id="fake-model", client=client)
    e.fit(["doc a", "doc b"])
    path = tmp_path / "emb.json"

    c1 = EmbeddingCache(path)
    for t in ("doc a", "doc b"):
        c1.get_or_embed(e.signature, t, e.embed)
    c1.save()
    assert client.calls == 2  # embedded once each

    c2 = EmbeddingCache(path)
    vecs = [c2.get_or_embed(e.signature, t, e.embed) for t in ("doc a", "doc b")]
    assert (c2.hits, c2.misses) == (2, 0)
    assert client.calls == 2  # cache hit — no new Bedrock calls
    assert vecs[0] == pytest.approx([1.0, 0.0])
