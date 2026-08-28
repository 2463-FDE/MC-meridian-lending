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

# The one outcome class that cannot be scored on retrieval rank. The client's
# clarification cases are ambiguous ACROSS documents by design — Q13's own
# rationale reads "Ambiguous across Adverse Action, Loan Review, and Credit
# cutoffs" — so they carry no single frozen anchor, in her CSV and in the
# authoritative JSONL alike. Scoring them against one chunk would grade a case
# on a target it was never given; folding them into `no_match` would inflate
# the abstention class, which is the one ratio proving abstention works. They
# are excluded from every score and reported as a count with the reason, the
# same treatment `UNMAPPED` gets for topics: the officer channel is a closed
# enum with free text masked at the boundary, so there is no ask-back path to
# exercise and scoring these would report coverage the product does not have.
UNSCORABLE_CLASS = "clarification"

# The support-test verdict states. The client's correction (S-1) makes the
# expected conclusion and the displayed summary two frozen targets graded
# separately, so there are two of these per case and they are never merged: a run
# that retrieves the right passage and states the wrong deadline is a different
# failure from one that states the right deadline off the wrong passage.
SUPPORTED = "supported"
UNSUPPORTED = "unsupported"
# S-9. Counts neither way. Defaulting an uncertain verdict to `UNSUPPORTED` would
# score "we could not tell" as "the system was wrong", which she ruled out by name.
HUMAN_REVIEW = "human_review"
# Not a verdict — the absence of one. Every case starts here and only a check
# that actually ran moves it, so an ungraded case can never read as a pass. The
# design doc's rule: "a field that is loaded but unscored reads as coverage that
# does not exist."
NOT_EVALUATED = "not_evaluated"
VERDICT_STATES = (SUPPORTED, UNSUPPORTED, HUMAN_REVIEW, NOT_EVALUATED)


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
    # Two independent support verdicts, never combined into one score.
    conclusion_verdict: str = NOT_EVALUATED
    summary_verdict: str = NOT_EVALUATED
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
        if not self.scorable:
            # Not a failure — an absence of a scoring target. Zeroed so a caller
            # reading these directly cannot read a stale hit as a result.
            self.hits = {k: False for k in K_VALUES}
            self.reciprocal_rank = 0.0
            self.correct = False

    @property
    def scorable(self) -> bool:
        """Whether this case has a retrieval target it can be graded against."""
        return self.outcome_class != UNSCORABLE_CLASS


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
class SupportStat:
    """One support target's verdict counts and graded rate.

    S-1: never merged with the other target. S-9: `human_review` counts in
    neither `n_graded` nor `rate` — only `supported`/`unsupported` are graded.
    """

    counts: dict[str, int]  # by VERDICT_STATES, omitting states with no cases
    n_graded: int  # supported + unsupported
    rate: float | None  # supported / n_graded, or None ("n/a") when n_graded == 0


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
    n_unscorable: int  # cases with no retrieval target (see UNSCORABLE_CLASS)
    # Counted per state and kept apart, because S-1 forbids one merged number.
    conclusion_verdicts: SupportStat
    summary_verdicts: SupportStat


def _support_stat(values) -> SupportStat:
    """Verdict counts plus the graded rate, omitting states with no cases."""
    counts = {state: 0 for state in VERDICT_STATES}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    n_graded = counts[SUPPORTED] + counts[UNSUPPORTED]
    rate = counts[SUPPORTED] / n_graded if n_graded else None
    return SupportStat(
        counts={k: n for k, n in counts.items() if n},
        n_graded=n_graded,
        rate=rate,
    )


def aggregate(evals: list[QueryEval]) -> Aggregate:
    # Unscorable cases leave EVERY denominator, not just hit@k: a case with no
    # target must not dilute a rate, appear as a scored topic row, or land in
    # the abstention count. It is reported on its own line instead.
    scorable = [e for e in evals if e.scorable]
    answerable = [e for e in scorable if not e.unanswerable]
    unanswerable = [e for e in scorable if e.unanswerable]
    n = len(answerable)
    by_topic: dict[str, TopicStat] = {}
    by_class: dict[str, ClassStat] = {}
    for e in scorable:
        stat = by_topic.setdefault(e.topic, TopicStat(n=0, correct=0))
        stat.n += 1
        stat.correct += int(e.correct)
        # `outcome_class` is resolved in __post_init__, so it is never None here.
        cstat = by_class.setdefault(str(e.outcome_class), ClassStat(n=0, correct=0))
        cstat.n += 1
        cstat.correct += int(e.correct)
    return Aggregate(
        by_topic=by_topic,
        n_unmapped=sum(1 for e in scorable if e.topic == UNMAPPED),
        n_unscorable=sum(1 for e in evals if not e.scorable),
        # Over ALL evals, not just scorable ones: a support verdict is about the
        # conclusion text, which exists independently of whether the case has a
        # retrieval target.
        conclusion_verdicts=_support_stat(e.conclusion_verdict for e in evals),
        summary_verdicts=_support_stat(e.summary_verdict for e in evals),
        n_answerable=n,
        n_unanswerable=len(unanswerable),
        hit_at_k={
            k: (sum(e.hits[k] for e in answerable) / n if n else 0.0) for k in K_VALUES
        },
        mrr=(sum(e.reciprocal_rank for e in answerable) / n if n else 0.0),
        unanswerable_correct=sum(e.correct for e in unanswerable),
        by_class=by_class,
    )
