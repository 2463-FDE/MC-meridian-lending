"""Retrieval metrics: hit@k, MRR, and unanswerable-query scoring (spec D1.5, DL-6).

Answerable queries score on rank of the first expected chunk. Unanswerable
queries (the #6012 class) score correct when the top retrieval score falls
below the confidence threshold — a data-capture gap, not a retrieval miss,
and the report routes them to the Data-gaps section (DL-6). The threshold is
calibrated empirically against the gold set, not pre-committed; the runner
records the chosen value and method in the report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

K_VALUES = (1, 3, 5)


@dataclass
class QueryEval:
    query_id: str
    query: str
    expected: list[str]          # expected chunk_ids; empty when unanswerable
    unanswerable: bool
    retrieved: list[tuple[str, float]]  # (chunk_id, score), ranked
    threshold: float
    hits: dict[int, bool] = field(init=False)
    reciprocal_rank: float = field(init=False)
    correct: bool = field(init=False)

    def __post_init__(self) -> None:
        ids = [cid for cid, _ in self.retrieved]
        top_score = self.retrieved[0][1] if self.retrieved else 0.0
        if self.unanswerable:
            # Correct = the harness does NOT confidently retrieve something.
            # Empty retrieval is always a correct abstention, whatever the
            # threshold (a degenerate corpus calibrates it to 0.0).
            self.hits = {k: False for k in K_VALUES}
            self.reciprocal_rank = 0.0
            self.correct = not self.retrieved or top_score < self.threshold
        else:
            self.hits = {k: any(cid in self.expected for cid in ids[:k]) for k in K_VALUES}
            rank = next((i + 1 for i, cid in enumerate(ids) if cid in self.expected), None)
            self.reciprocal_rank = 1.0 / rank if rank else 0.0
            self.correct = self.hits[max(K_VALUES)]


@dataclass
class Aggregate:
    n_answerable: int
    n_unanswerable: int
    hit_at_k: dict[int, float]       # over answerable queries
    mrr: float                       # over answerable queries
    unanswerable_correct: int        # count scored correct


def aggregate(evals: list[QueryEval]) -> Aggregate:
    answerable = [e for e in evals if not e.unanswerable]
    unanswerable = [e for e in evals if e.unanswerable]
    n = len(answerable)
    return Aggregate(
        n_answerable=n,
        n_unanswerable=len(unanswerable),
        hit_at_k={k: (sum(e.hits[k] for e in answerable) / n if n else 0.0) for k in K_VALUES},
        mrr=(sum(e.reciprocal_rank for e in answerable) / n if n else 0.0),
        unanswerable_correct=sum(e.correct for e in unanswerable),
    )
