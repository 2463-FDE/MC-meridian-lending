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

# A case no officer topic can express. Defined here rather than in run.py because
# the loader, the aggregate and the report all have to agree on the one spelling;
# a second copy is how a bucket silently splits in two.
UNMAPPED = "unmapped"


@dataclass
class QueryEval:
    query_id: str
    query: str
    expected: list[str]  # expected chunk_ids; empty when unanswerable
    unanswerable: bool
    retrieved: list[tuple[str, float]]  # (chunk_id, score), ranked
    threshold: float
    # The officer topic this case would be asked under. `unmapped` is a result,
    # not a default to be tidied away: seven of the client's questions are
    # servicing and collections questions and the closed officer vocabulary has
    # no code for either, so they cannot be asked through the product at all.
    # Seven, not the six her own needs_review set names: Q24's frozen anchor is
    # also in SYN-POL-SERVICING-COLLECTIONS.md, which that set missed.
    # The default is the safe answer for a directly-constructed eval, NOT the
    # value the committed gold set relies on — every committed case names its own
    # topic, and a test asserts it, because a set that defaults into `unmapped`
    # wholesale collapses the per-topic report to a single row.
    topic: str = UNMAPPED
    # Which of the client's four outcome classes this case belongs to. Scoring
    # is unchanged by it — `unanswerable` still decides how a case is scored —
    # but a class-blind aggregate hides a whole class going wrong, which is the
    # failure mode a corpus of near-identical scaffolding sections invites.
    # Absent on the committed gold set, where it follows from `unanswerable`.
    # Orthogonal to `topic`: one says what was asked, the other what came back.
    outcome_class: str | None = None
    hits: dict[int, bool] = field(init=False)
    reciprocal_rank: float = field(init=False)
    correct: bool = field(init=False)

    def __post_init__(self) -> None:
        if self.outcome_class is None:
            self.outcome_class = "no_match" if self.unanswerable else "answer"
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
            self.hits = {
                k: any(cid in self.expected for cid in ids[:k]) for k in K_VALUES
            }
            rank = next(
                (i + 1 for i, cid in enumerate(ids) if cid in self.expected), None
            )
            self.reciprocal_rank = 1.0 / rank if rank else 0.0
            self.correct = self.hits[max(K_VALUES)]


@dataclass
class TopicStat:
    """One topic's own count and score, so no topic hides inside a pooled mean."""

    n: int
    correct: int


@dataclass
class ClassStat:
    """One outcome class's own count and score, so no class hides in the mean."""

    n: int
    correct: int


@dataclass
class Aggregate:
    n_answerable: int
    n_unanswerable: int
    hit_at_k: dict[int, float]  # over answerable queries
    mrr: float  # over answerable queries
    unanswerable_correct: int  # count scored correct
    by_topic: dict[str, TopicStat]  # per officer topic, insertion-ordered
    n_unmapped: int  # cases no officer topic can express
    by_class: dict[str, ClassStat]  # per outcome class, insertion-ordered


def aggregate(evals: list[QueryEval]) -> Aggregate:
    answerable = [e for e in evals if not e.unanswerable]
    unanswerable = [e for e in evals if e.unanswerable]
    n = len(answerable)
    by_topic: dict[str, TopicStat] = {}
    by_class: dict[str, ClassStat] = {}
    for e in evals:
        stat = by_topic.setdefault(e.topic, TopicStat(n=0, correct=0))
        stat.n += 1
        stat.correct += int(e.correct)
        # `outcome_class` is resolved in __post_init__, so it is never None here.
        cstat = by_class.setdefault(str(e.outcome_class), ClassStat(n=0, correct=0))
        cstat.n += 1
        cstat.correct += int(e.correct)
    return Aggregate(
        by_topic=by_topic,
        n_unmapped=sum(1 for e in evals if e.topic == "unmapped"),
        n_answerable=n,
        n_unanswerable=len(unanswerable),
        hit_at_k={
            k: (sum(e.hits[k] for e in answerable) / n if n else 0.0) for k in K_VALUES
        },
        mrr=(sum(e.reciprocal_rank for e in answerable) / n if n else 0.0),
        unanswerable_correct=sum(e.correct for e in unanswerable),
        by_class=by_class,
    )
