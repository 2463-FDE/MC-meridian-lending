"""Unit tests for the in-memory index and retrieval metrics (spec D1.5, DL-6/DL-7)."""

from rag_eval.embedder import TfidfEmbedder
from rag_eval.index import InMemoryIndex
from rag_eval.metrics import K_VALUES, QueryEval, aggregate

CORPUS = [
    "Approve: model score >= 660 and DTI <= 43 percent",
    "Late payment fee 35 dollars flat or 5 percent of past-due amount whichever is less",
    "Interest accrues on outstanding principal at the note rate",
]


def _index():
    e = TfidfEmbedder()
    e.fit(CORPUS)
    idx = InMemoryIndex()
    for i, text in enumerate(CORPUS):
        idx.add(f"chunk-{i}", e.embed(text))
    return e, idx


def test_search_ranks_matching_chunk_first():
    e, idx = _index()
    results = idx.search(e.embed("what is the late payment fee?"), k=3)
    assert results[0][0] == "chunk-1"
    assert results[0][1] > 0


def test_search_returns_at_most_k():
    e, idx = _index()
    assert len(idx.search(e.embed("fee"), k=2)) == 2
    assert len(idx.search(e.embed("fee"), k=10)) == 3


def test_search_deterministic_tiebreak_on_chunk_id():
    idx = InMemoryIndex()
    vec = {"term": 1.0}
    idx.add("b", vec)
    idx.add("a", vec)
    results = idx.search({"term": 1.0}, k=2)
    assert [cid for cid, _ in results] == ["a", "b"]


def test_hit_at_k_and_mrr_expected_at_rank_2():
    q = QueryEval(
        query_id="q1", query="x", expected=["good"], unanswerable=False,
        retrieved=[("other", 0.9), ("good", 0.8), ("more", 0.1)], threshold=0.1,
    )
    assert q.hits == {1: False, 3: True, 5: True}
    assert q.reciprocal_rank == 0.5
    assert q.correct


def test_expected_not_retrieved_scores_zero():
    q = QueryEval(
        query_id="q2", query="x", expected=["missing"], unanswerable=False,
        retrieved=[("a", 0.9), ("b", 0.8)], threshold=0.1,
    )
    assert q.hits == {k: False for k in K_VALUES}
    assert q.reciprocal_rank == 0.0
    assert not q.correct


def test_unanswerable_correct_when_top_score_below_threshold():
    q = QueryEval(
        query_id="q6012", query="why was application 6012 denied?", expected=[],
        unanswerable=True, retrieved=[("chunk-0", 0.05)], threshold=0.2,
    )
    assert q.correct
    assert q.reciprocal_rank == 0.0


def test_unanswerable_incorrect_when_confidently_retrieved():
    q = QueryEval(
        query_id="q6012", query="why was application 6012 denied?", expected=[],
        unanswerable=True, retrieved=[("chunk-0", 0.9)], threshold=0.2,
    )
    assert not q.correct


def test_unanswerable_with_empty_retrieval_is_correct():
    q = QueryEval(
        query_id="q-off", query="off corpus", expected=[],
        unanswerable=True, retrieved=[], threshold=0.2,
    )
    assert q.correct


def test_aggregate_splits_answerable_and_unanswerable():
    evals = [
        QueryEval("a1", "x", ["c"], False, [("c", 0.9)], 0.1),
        QueryEval("a2", "x", ["c"], False, [("d", 0.9), ("c", 0.8)], 0.1),
        QueryEval("u1", "x", [], True, [("c", 0.05)], 0.2),
    ]
    agg = aggregate(evals)
    assert agg.n_answerable == 2
    assert agg.n_unanswerable == 1
    assert agg.hit_at_k[1] == 0.5
    assert agg.hit_at_k[3] == 1.0
    assert agg.mrr == 0.75
    assert agg.unanswerable_correct == 1


def test_aggregate_empty_answerable_no_division_error():
    agg = aggregate([QueryEval("u1", "x", [], True, [], 0.2)])
    assert agg.hit_at_k[1] == 0.0
    assert agg.mrr == 0.0
