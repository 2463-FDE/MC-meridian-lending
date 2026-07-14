"""Unit tests for the runner: threshold calibration + gate enforcement (spec D2.4, DL-6)."""

import hashlib
import io
import json
from pathlib import Path

from rag_eval.embedder import BedrockEmbedder
from rag_eval.run import calibrate_threshold, make_embedder, run

REPO = Path(__file__).resolve().parents[2]


def test_calibrate_picks_minimal_error_split():
    # answerable tops all >= 0.3, unanswerable all <= 0.1: clean separation.
    t = calibrate_threshold([0.5, 0.3, 0.4], [0.1, 0.05])
    assert 0.1 < t < 0.3


def test_calibrate_overlapping_scores_minimizes_errors():
    # One unanswerable (0.35) sits above the weakest answerable (0.25):
    # best split leaves exactly one error, below 0.25.
    t = calibrate_threshold([0.25, 0.5, 0.6], [0.35, 0.05])
    assert 0.05 < t < 0.25


def test_calibrate_degenerate_single_point():
    assert calibrate_threshold([0.5], []) == 0.0
    assert calibrate_threshold([], []) == 0.0


def test_gate_blocks_contaminated_file_from_index(tmp_path: Path):
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "clean.md").write_text(
        "# Clean Policy\n\n## Fees\n\nLate payment fee is $35 flat.\n",
        encoding="utf-8",
    )
    (policies / "dirty.md").write_text(
        "# Leaky Doc\n\n## Applicant\n\nSSN 123-45-6789 on file.\n",
        encoding="utf-8",
    )

    result = run(base=tmp_path)

    by_path = {Path(v.path).name: v for v in result.verdicts}
    assert by_path["clean.md"].passed
    assert not by_path["dirty.md"].passed
    # Only the clean file's single section was chunked/embedded (D2.4).
    assert result.n_chunks == 1
    # Refused file's content never reaches cache or report in raw form.
    cache_text = (tmp_path / "rag_eval" / ".cache" / "embeddings.json").read_text()
    assert "123-45-6789" not in cache_text
    assert "123-45-6789" not in result.report_text
    # Hygiene verdicts do appear in the report (D2.5), masked.
    assert "dirty.md" in result.report_text
    assert "REFUSED" in result.report_text


def test_main_exits_nonzero_on_refused_policy_file(tmp_path: Path):
    # A NEW contaminated policy doc must break the CI gate — not be silently
    # excluded while the run stays green (fail-closed security control).
    import pytest

    from rag_eval.run import main

    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "clean.md").write_text(
        "# Clean\n\n## Fees\n\nLate payment fee is $35 flat.\n", encoding="utf-8"
    )
    (policies / "dirty.md").write_text(
        "# Leaky\n\n## Applicant\n\nSSN 123-45-6789 on file.\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit) as e:
        main(base=tmp_path)
    assert e.value.code == 1
    # The report is still written so the refusal is diagnosable.
    assert (tmp_path / "rag_eval" / "eval_report.md").exists()


def test_legacy_dump_expected_only_at_baseline_hash():
    # The real, unmodified legacy dump is the one tolerated refusal.
    from rag_eval.run import _refusal_is_expected

    real = REPO / "kb_dump" / "applications.jsonl"
    assert _refusal_is_expected(str(real), REPO)


def test_duplicate_nested_legacy_copy_is_not_expected(tmp_path: Path):
    # A second copy of the legacy dump at a nested path with identical (approved)
    # content must NOT inherit the exception — only the exact canonical path does.
    import pytest

    from rag_eval.run import _refusal_is_expected, main

    real = (REPO / "kb_dump" / "applications.jsonl").read_bytes()
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "clean.md").write_text(
        "# Clean\n\n## Fees\n\nLate payment fee is $35 flat.\n", encoding="utf-8"
    )
    canonical = tmp_path / "kb_dump" / "applications.jsonl"
    canonical.parent.mkdir()
    canonical.write_bytes(real)
    dupe = tmp_path / "kb_dump" / "archive" / "kb_dump" / "applications.jsonl"
    dupe.parent.mkdir(parents=True)
    dupe.write_bytes(real)

    assert _refusal_is_expected(str(canonical), tmp_path)
    assert not _refusal_is_expected(str(dupe), tmp_path)  # same content, wrong path
    with pytest.raises(SystemExit) as e:
        main(base=tmp_path)  # the nested duplicate trips the gate
    assert e.value.code == 1


def test_main_fails_when_legacy_dump_is_tampered(tmp_path: Path):
    # Adding fresh PII to the legacy dump changes its hash: the path-only
    # exception no longer covers it, so the gate must fail closed.
    import pytest

    from rag_eval.run import main

    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "clean.md").write_text(
        "# Clean\n\n## Fees\n\nLate payment fee is $35 flat.\n", encoding="utf-8"
    )
    (tmp_path / "kb_dump").mkdir()
    # Not the approved baseline content -> hash mismatch -> unexpected refusal.
    (tmp_path / "kb_dump" / "applications.jsonl").write_text(
        '{"ssn": "999-88-7777", "pan": "4111111111111111"}\n', encoding="utf-8"
    )
    with pytest.raises(SystemExit) as e:
        main(base=tmp_path)
    assert e.value.code == 1


def test_recursive_scan_catches_contaminated_subdir_policy(tmp_path: Path):
    # A markdown doc in a policies/ SUBDIRECTORY must be scanned (not skipped by
    # a shallow glob) and, if contaminated, fail the gate closed.
    import pytest

    from rag_eval.run import main

    sub = tmp_path / "policies" / "2026"
    sub.mkdir(parents=True)
    (tmp_path / "policies" / "clean.md").write_text(
        "# Clean\n\n## Fees\n\nLate payment fee is $35 flat.\n", encoding="utf-8"
    )
    (sub / "leaky.md").write_text(
        "# Leaky\n\n## Card\n\ncvv: 123 and SSN 123-45-6789.\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit) as e:
        main(base=tmp_path)
    assert e.value.code == 1


def test_unsupported_kb_dump_file_fails_gate(tmp_path: Path):
    # A non-.md/.jsonl file under a corpus root (e.g. a CSV export) must be
    # scanned and fail the gate, not bypass it on an extension filter.
    import pytest

    from rag_eval.run import main

    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "clean.md").write_text(
        "# Clean\n\n## Fees\n\nLate payment fee is $35 flat.\n", encoding="utf-8"
    )
    (tmp_path / "kb_dump").mkdir()
    (tmp_path / "kb_dump" / "customers.csv").write_text(
        "name,ssn\nAlice,123-45-6789\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit) as e:
        main(base=tmp_path)
    assert e.value.code == 1


def test_duplicate_stem_across_dirs_raises(tmp_path: Path):
    # Two clean docs sharing a filename stem collide on the doc# id prefix.
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "fees.md").write_text(
        "# A\n\n## Late\n\nLate fee $35.\n", encoding="utf-8"
    )
    sub = tmp_path / "policies" / "archive"
    sub.mkdir()
    (sub / "fees.md").write_text("# B\n\n## Late\n\nLate fee $40.\n", encoding="utf-8")
    import pytest

    with pytest.raises(RuntimeError, match="duplicate chunk ids across corpus"):
        run(base=tmp_path)


def test_gold_query_with_pii_fails_closed(tmp_path: Path, monkeypatch):
    # A gold query carrying PII must not be embedded (external API on Bedrock)
    # or written to the report — the runner scans queries and fails closed.
    import pytest

    from rag_eval import run as run_mod

    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "clean.md").write_text(
        "# Clean\n\n## Fees\n\nLate payment fee is $35 flat.\n", encoding="utf-8"
    )
    gold = tmp_path / "gold.json"
    gold.write_text(
        json.dumps(
            {"queries": [{"id": "q-pii", "query": "why was ssn 412-55-9981 denied?"}]}
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(run_mod, "GOLD_PATH", gold)

    with pytest.raises(RuntimeError, match="contain PII") as e:
        run_mod.run(base=tmp_path)
    assert "412-55-9981" not in str(e.value)  # position only, never a value
    report = tmp_path / "rag_eval" / "eval_report.md"
    if report.exists():
        assert "412-55-9981" not in report.read_text()
    # Validation runs BEFORE any embedding/cache side effect: no cache written.
    assert not (tmp_path / "rag_eval" / ".cache" / "embeddings.json").exists()


def test_gold_pii_in_id_or_expected_fails_without_echo(tmp_path: Path, monkeypatch):
    # PII in id or expected (both printed in the report) must fail closed, and
    # the error must not echo the offending value.
    import pytest

    from rag_eval import run as run_mod

    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "clean.md").write_text(
        "# Clean\n\n## Fees\n\nLate payment fee is $35 flat.\n", encoding="utf-8"
    )
    gold = tmp_path / "gold.json"
    gold.write_text(
        json.dumps(
            {
                "queries": [
                    {"id": "ssn-412-55-9981", "query": "approve cutoff?"},
                    {"id": "q2", "query": "dti?", "expected": ["ssn 330-90-5512"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(run_mod, "GOLD_PATH", gold)

    with pytest.raises(RuntimeError, match="contain PII") as e:
        run_mod.run(base=tmp_path)
    msg = str(e.value)
    assert "412-55-9981" not in msg and "330-90-5512" not in msg
    assert not (tmp_path / "rag_eval" / ".cache" / "embeddings.json").exists()


def test_embed_failure_purges_stale_cache(tmp_path: Path, monkeypatch):
    # A mid-loop embed failure (e.g. Bedrock timeout) must not leave a prior
    # run's PII-bearing cache on disk — save()/prune never runs on this path.
    import pytest

    from rag_eval import run as run_mod

    class _BoomEmbedder:
        def __init__(self):
            self.signature = ""

        def fit(self, texts):
            self.signature = "boom-v1"

        def embed(self, text):
            raise RuntimeError("bedrock timeout")

    monkeypatch.setattr(run_mod, "make_embedder", lambda: _BoomEmbedder())

    cache = tmp_path / "rag_eval" / ".cache" / "embeddings.json"
    cache.parent.mkdir(parents=True)
    cache.write_text('{"stale-key": {"ssn": 1.0}}', encoding="utf-8")
    (tmp_path / "policies").mkdir()
    (tmp_path / "policies" / "clean.md").write_text(
        "# Clean\n\n## Fees\n\nLate payment fee is $35 flat.\n", encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="bedrock timeout"):
        run_mod.run(base=tmp_path)
    assert not cache.exists()  # stale cache purged on the failure path


def test_pii_in_filename_fails_before_artifacts(tmp_path: Path):
    # A clean-content file whose NAME carries PII (SSN) must fail closed before
    # the report/cache are written, and the raw name must not be echoed.
    import pytest

    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "Jane-Doe-330-90-5512.md").write_text(
        "# Doc\n\n## Fees\n\nLate payment fee is $35 flat.\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="contain PII in their names") as e:
        run(base=tmp_path)
    assert "330-90-5512" not in str(e.value)  # position only, name not echoed
    assert not (tmp_path / "rag_eval" / "eval_report.md").exists()
    assert not (tmp_path / "rag_eval" / ".cache" / "embeddings.json").exists()


def test_empty_corpus_aborts_loudly(tmp_path: Path):
    # Teeth finding: no corpus must not yield a plausible-looking empty report.
    import pytest

    with pytest.raises(RuntimeError, match="no gate-passed corpus"):
        run(base=tmp_path)


def test_all_refused_corpus_aborts_loudly(tmp_path: Path):
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "dirty.md").write_text(
        "# Leaky\n\n## A\n\nSSN 123-45-6789.\n", encoding="utf-8"
    )
    import pytest

    with pytest.raises(RuntimeError, match="1 refused"):
        run(base=tmp_path)


def test_all_refused_run_purges_stale_cache(tmp_path: Path):
    # The abort path never reaches cache.save() (which prunes), so it must purge
    # the prior run's cache itself — else PII-bearing vectors from a now-refused
    # document linger on disk.
    import pytest

    cache = tmp_path / "rag_eval" / ".cache" / "embeddings.json"
    cache.parent.mkdir(parents=True)
    cache.write_text('{"stale-key": {"ssn": 1.0}}', encoding="utf-8")

    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "dirty.md").write_text(
        "# Leaky\n\n## A\n\nSSN 123-45-6789.\n", encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="no gate-passed corpus"):
        run(base=tmp_path)
    assert not cache.exists()  # stale cache purged before the abort


def test_second_run_hits_cache_entirely(tmp_path: Path):
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "clean.md").write_text(
        "# Clean Policy\n\n## Fees\n\nLate payment fee is $35 flat.\n",
        encoding="utf-8",
    )
    first = run(base=tmp_path)
    assert first.cache_misses == first.n_chunks
    second = run(base=tmp_path)
    assert second.cache_misses == 0
    assert second.cache_hits == second.n_chunks


# --- Bedrock backend end-to-end (fake client, no real API) ---


class _DeterministicBedrockClient:
    """Answers ANY input text with a stable 8-dim vector derived from its hash,
    so the pipeline runs over arbitrary chunk/query text without canned data."""

    def __init__(self):
        self.calls = 0

    def invoke_model(self, modelId, body):  # noqa: N803 — boto3's kwarg name
        self.calls += 1
        text = json.loads(body)["inputText"]
        digest = hashlib.sha256(text.encode()).digest()
        vec = [digest[i] / 255.0 for i in range(8)]
        payload = json.dumps({"embedding": vec})
        return {"body": io.BytesIO(payload.encode())}


def test_run_with_bedrock_backend(tmp_path: Path, monkeypatch):
    # Dense vectors must flow gate -> cache -> index -> search -> metrics ->
    # report with no type errors, and the report must name the backend.
    policies = tmp_path / "policies"
    policies.mkdir()
    (policies / "clean.md").write_text(
        "# Clean Policy\n\n## Fees\n\nLate payment fee is $35 flat.\n"
        "\n## Approval\n\nApprove when model score is at least 660.\n",
        encoding="utf-8",
    )

    def fake_make_embedder():
        return BedrockEmbedder(
            model_id="fake-model", client=_DeterministicBedrockClient()
        )

    monkeypatch.setattr("rag_eval.run.make_embedder", fake_make_embedder)

    result = run(base=tmp_path)
    assert result.n_chunks == 2
    assert result.embedder_signature == "bedrock-v1:fake-model"
    assert "bedrock-v1:fake-model" in result.report_text
    # Dense vectors landed in the cache as JSON lists, not sparse dicts.
    cached = json.loads(
        (tmp_path / "rag_eval" / ".cache" / "embeddings.json").read_text()
    )
    assert all(isinstance(v, list) for v in cached.values())


def test_make_embedder_default_is_tfidf(monkeypatch):
    monkeypatch.delenv("RAG_EMBEDDER", raising=False)
    from rag_eval.embedder import TfidfEmbedder

    assert isinstance(make_embedder(), TfidfEmbedder)


def test_make_embedder_rejects_unknown(monkeypatch):
    import pytest

    monkeypatch.setenv("RAG_EMBEDDER", "word2vec")
    with pytest.raises(ValueError, match="RAG_EMBEDDER"):
        make_embedder()
