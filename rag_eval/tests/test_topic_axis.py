"""Per-topic scoring: the officer vocabulary as a reporting axis.

The client asked for results reported by topic rather than as one pooled number,
and supplied a draft question-to-topic mapping. Two things follow.

A gold case may name the topic an officer would have selected. That topic must
belong to the closed officer vocabulary, because a code the product cannot emit
is a case the product cannot be asked — scoring it would report coverage the
system does not have.

Six of her questions are servicing and collections questions, and the vocabulary
has no code for either. They carry `unmapped`, which is a result rather than a
gap to paper over: those questions cannot be asked through the product at all,
and they are excluded from topic scoring with the denominator stated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rag_eval import run as run_mod
from rag_eval.metrics import QueryEval, aggregate


def _corpus(tmp_path: Path) -> Path:
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "fees.md").write_text(
        "# Fees\n\n## Late Fee\n\nLate payment fee is $35 flat.\n", encoding="utf-8"
    )
    return tmp_path


def _gold(path: Path, queries: list[dict]) -> Path:
    path.write_text(json.dumps({"queries": queries}), encoding="utf-8")
    return path


def test_topic_vocabulary_matches_the_product(tmp_path: Path):
    # The harness cannot import a service, so the vocabulary is duplicated. A
    # copy that silently drifts would score against codes the officer channel no
    # longer offers -- the same failure the redactor drift gate exists to catch.
    product = (
        Path(__file__).resolve().parents[2]
        / "services"
        / "origination-service"
        / "app"
        / "policy_retrieval.py"
    )
    source = product.read_text(encoding="utf-8")
    block = source.split("POLICY_TOPICS = (", 1)[1].split(")", 1)[0]
    codes = {line.strip().strip('",') for line in block.splitlines() if line.strip()}

    assert codes == set(run_mod.TOPIC_CODES), (
        "rag_eval.run.TOPIC_CODES has drifted from POLICY_TOPICS in "
        "origination-service; resync before scoring against it"
    )


def test_gold_accepts_a_topic_from_the_vocabulary(tmp_path: Path, monkeypatch):
    base = _corpus(tmp_path)
    gold = _gold(
        tmp_path / "gold.json",
        [
            {
                "id": "q-fee",
                "query": "fee schedule",
                "expected": ["fees#late-fee"],
                "topic": "fee_schedule",
            }
        ],
    )

    monkeypatch.setattr(run_mod, "GOLD_PATH", gold)
    result = run_mod.run(base=base)

    assert result.evals[0].topic == "fee_schedule"


def test_gold_accepts_unmapped(tmp_path: Path, monkeypatch):
    # Six servicing/collections questions have no code. `unmapped` records that
    # rather than filing them under a nearest-fit code that would score.
    base = _corpus(tmp_path)
    gold = _gold(
        tmp_path / "gold.json",
        [
            {
                "id": "q-servicing",
                "query": "first-party collections",
                "expected": ["fees#late-fee"],
                "topic": "unmapped",
            }
        ],
    )

    monkeypatch.setattr(run_mod, "GOLD_PATH", gold)
    result = run_mod.run(base=base)

    assert result.evals[0].topic == "unmapped"


def test_gold_rejects_a_topic_outside_the_vocabulary(tmp_path: Path, monkeypatch):
    # A code the officer channel cannot emit is a case that cannot be asked.
    base = _corpus(tmp_path)
    gold = _gold(
        tmp_path / "gold.json",
        [
            {
                "id": "q-bad",
                "query": "servicing",
                "expected": ["fees#late-fee"],
                "topic": "servicing_collections",
            }
        ],
    )

    monkeypatch.setattr(run_mod, "GOLD_PATH", gold)
    with pytest.raises(RuntimeError, match="topic"):
        run_mod.run(base=base)


def test_aggregate_reports_per_topic_and_excludes_unmapped():
    evals = [
        QueryEval(
            query_id="a",
            query="q",
            expected=["d#s"],
            unanswerable=False,
            retrieved=[("d#s", 0.9)],
            threshold=0.5,
            topic="adverse_action",
        ),
        QueryEval(
            query_id="b",
            query="q",
            expected=["d#s"],
            unanswerable=False,
            retrieved=[("d#x", 0.9)],
            threshold=0.5,
            topic="adverse_action",
        ),
        QueryEval(
            query_id="c",
            query="q",
            expected=["d#s"],
            unanswerable=False,
            retrieved=[("d#s", 0.9)],
            threshold=0.5,
            topic="unmapped",
        ),
    ]

    agg = aggregate(evals)

    assert agg.by_topic["adverse_action"].n == 2
    assert agg.by_topic["adverse_action"].correct == 1
    # `unmapped` is counted and shown, never folded into a topic's score.
    assert agg.by_topic["unmapped"].n == 1
    assert agg.n_unmapped == 1


def test_topic_defaults_to_unmapped_when_absent():
    # The committed gold set carries no topic. It must not silently join a topic.
    e = QueryEval(
        query_id="q",
        query="q",
        expected=["d#s"],
        unanswerable=False,
        retrieved=[("d#s", 0.9)],
        threshold=0.5,
    )

    assert e.topic == "unmapped"


def test_report_renders_the_per_topic_table(tmp_path: Path, monkeypatch):
    base = _corpus(tmp_path)
    gold = _gold(
        tmp_path / "gold.json",
        [
            {
                "id": "q-fee",
                "query": "fee schedule",
                "expected": ["fees#late-fee"],
                "topic": "fee_schedule",
            }
        ],
    )

    monkeypatch.setattr(run_mod, "GOLD_PATH", gold)
    result = run_mod.run(base=base)

    assert "| Topic | Cases | Correct |" in result.report_text
    assert "| `fee_schedule` | 1 |" in result.report_text
