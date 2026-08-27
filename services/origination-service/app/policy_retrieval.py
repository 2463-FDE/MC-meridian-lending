"""Policy retrieval over the committed corpus (ADR 0019).

The assistant's `search_policy` tool calls `search()`. The model chooses the query; this
module decides what is true and hands back the corpus text VERBATIM for the officer. The
model never sees that text — the loop feeds it only the status code and the score (ADR 0019
decision 3), which is why nothing here is shaped for a prompt.

Everything fails closed to an abstention: an unimportable `rag_eval`, no corpus, a corpus
file the ADR 0007 hygiene scan refuses, an unset or malformed threshold, an empty query, or a
best match below the threshold all return `PolicyAnswer(status="policy_abstain", ...)`. An officer then reads "no policy
match", which is honest; a fabricated or unvouched-for quotation would not be.

The index is built once per process from `rag_eval` (importable in the container since card
G2a) and kept in memory — 9 chunks, exact cosine (ADR 0007 rule 6, debt D16).
"""

import threading
from dataclasses import dataclass
from pathlib import Path

try:
    from rag_eval.chunker import chunk_markdown
    from rag_eval.hygiene import scan_file
    from rag_eval.index import InMemoryIndex
    from rag_eval.run import (
        audit_corpus_against_manifest,
        load_corpus_manifest,
        make_embedder,
        unsafe_corpus_path_reason,
    )
except ImportError as exc:
    # `rag_eval` is repo-root, and card G2a puts it in the IMAGE (copied to /app/rag_eval,
    # WORKDIR /app) — it does not put it on the sys.path of every checkout. A bare
    # `python -c "import app.main"` from this directory has neither the image layout nor
    # pytest.ini's `pythonpath = ../..`, which is exactly what CI's `backend` import smoke
    # runs. Importing origination must not depend on the assistant's optional retrieval
    # tool, so record the failure and abstain at call time instead of taking the service
    # down. rag-eval-import-gate proves the import inside the shipped image, so a
    # container that silently lost the harness still fails CI.
    _HARNESS_IMPORT_ERROR: ImportError | None = exc
else:
    _HARNESS_IMPORT_ERROR = None

from . import config
from .logging_config import get_logger

log = get_logger("policy_retrieval")

# Abstention reasons. Operational codes for the log and the tests — the model sees only
# `status`, so these never cross the LLM boundary.
# The officer-selectable policy topics, one per retrievable section of the committed
# corpus. This is a CLOSED vocabulary, and it is the officer's whole channel into
# retrieval: the boundary masks free text (an officer question would arrive at the model
# as a redaction placeholder, which is why one was never plumbed), and an enum code
# passes it intact. Each code is a compound operational token, so none can collide with
# a plausible bare name — the property `_SAFE_CATEGORICAL` depends on.
#
# Tied to the corpus, not invented: `test_policy_topic.py` asserts every code here
# actually retrieves a hit, so a topic cannot survive the section behind it being
# renamed or dropped. Adding a code without corpus text to back it fails that test.
#
# Duplicated into `llm/request_builder._SAFE_CATEGORICAL` rather than imported, with a
# parity test: `app/llm/` deliberately depends on nothing in the app domain, and
# importing this module there would drag `rag_eval` into the redaction path.
POLICY_TOPICS = (
    "fee_schedule",
    "apr_finance_charge",
    "interest_rate",
    "eligibility_rules",
    "credit_decisioning",
    "adverse_action",
    "debt_to_income",
    "records_retention",
)

NO_THRESHOLD = "threshold_unset"
NO_CORPUS = "corpus_unavailable"
EMPTY_QUERY = "empty_query"
BELOW_THRESHOLD = "below_threshold"
HARNESS_UNAVAILABLE = "harness_unavailable"
DECISION_TASK = "refused_on_decision_task"

_HIT = "policy_hit"
_ABSTAIN = "policy_abstain"


@dataclass(frozen=True)
class PolicyAnswer:
    """One retrieval result. `text`/`chunk_id` are officer-facing only."""

    status: str
    score: float = 0.0
    chunk_id: str = ""
    text: str = ""
    reason: str = ""

    @property
    def is_hit(self) -> bool:
        return self.status == _HIT

    def tool_result(self) -> dict:
        """The projection the model may see: an allowlisted status and a number.

        Deliberately excludes chunk_id and text. `_SAFE_CATEGORICAL` would mask the prose
        anyway, but relying on the mask would make the boundary an accident of the redactor
        rather than a property of this contract.
        """
        return {"status": self.status, "score": round(self.score, 4)}


def abstain(reason: str, score: float = 0.0) -> PolicyAnswer:
    return PolicyAnswer(status=_ABSTAIN, score=score, reason=reason)


_lock = threading.Lock()
_index_state = None  # (InMemoryIndex, embedder, {chunk_id: text}) once built


def corpus_dir() -> Path:
    """The corpus directory: configured, else the mount, else the checkout.

    /app/policies is where docker-compose.yml bind-mounts ./policies read-only (WORKDIR is
    /app); <repo>/policies is where it lives in a checkout, which is what the tests read.
    """
    if config.POLICY_CORPUS_DIR:
        return Path(config.POLICY_CORPUS_DIR)
    parents = Path(__file__).resolve().parents
    # parents[1] is /app in the image and services/origination-service in a checkout;
    # parents[3] is the repo root, which EXISTS ONLY in the checkout — the image bottoms
    # out at / after three levels, so indexing it unguarded raises IndexError there.
    candidates = [parents[1] / "policies"]
    if len(parents) > 3:
        candidates.append(parents[3] / "policies")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def reset_index_cache() -> None:
    """Drop the built index (tests, and any future corpus reload)."""
    global _index_state
    with _lock:
        _index_state = None


def _load_corpus() -> list:
    """Chunk every corpus document the hygiene gate passes.

    ADR 0007 gates the COMMITTED corpus in CI; this scan covers the copy that actually
    arrives, since it comes over a bind mount that a deployment could point elsewhere. A
    file with a finding is skipped and named in the log by path and PII type — never by
    value, which is the whole point of the scan.

    A file that cannot be read or chunked is skipped for the same reason: an unreadable
    corpus file must not take the assistant down, and it must not be quoted either.
    """
    chunks = []
    base = corpus_dir()
    manifest = None
    if config.POLICY_CORPUS_MANIFEST:
        # Fail closed on every manifest problem: an unreadable or malformed
        # declaration, or a corpus that does not match it, yields no corpus and
        # therefore an abstention. Indexing the listed subset of a mismatched
        # directory would serve a different corpus than the one approved, which
        # is worse than answering "no policy match".
        try:
            manifest = load_corpus_manifest(Path(config.POLICY_CORPUS_MANIFEST))
        except (OSError, ValueError) as exc:
            log.warning(
                "policy corpus manifest unusable, indexing nothing: %s", exc
            )
            return []
        problems = audit_corpus_against_manifest(base, manifest)
        if problems:
            log.warning(
                "policy corpus does not match its manifest, indexing nothing: %s",
                "; ".join(problems),
            )
            return []
    for path in sorted(base.glob("*.md")):
        # The hygiene gate below scans CONTENT only. A file mounted with clean
        # content but an unsafe NAME (jane-doe-123-45-6789.md) would still pass
        # it, then leak identity through the officer-visible chunk id (doc =
        # path.stem, ADR 0007 rule 6) and this loop's own log lines. ADR 0007's
        # CI-time scan never sees this bind-mounted copy, so re-check the path
        # here with the same function rag_eval.run applies to the committed
        # corpus (rag_eval/run.py::run).
        reason = unsafe_corpus_path_reason(
            path.relative_to(base), base=base, manifest=manifest
        )
        if reason is not None:
            # The filename itself is the flagged problem — unlike the branches
            # below, path.name is not safe to log here.
            log.warning(
                "policy corpus file REFUSED for an unsafe name (%s), not "
                "indexed (name withheld)",
                reason,
            )
            continue
        try:
            verdict = scan_file(path)
        except OSError as exc:
            log.warning(
                "policy corpus file unreadable, skipped: %s (%s)", path.name, exc
            )
            continue
        if not verdict.passed:
            log.warning(
                "policy corpus file REFUSED by the hygiene gate, not indexed: %s (%s)",
                path.name,
                verdict.counts(),
            )
            continue
        try:
            chunks.extend(chunk_markdown(path))
        except (OSError, ValueError) as exc:
            log.warning(
                "policy corpus file not chunkable, skipped: %s (%s)", path.name, exc
            )
    return chunks


def _build_index():
    chunks = _load_corpus()
    if not chunks:
        return None
    embedder = make_embedder()
    embedder.fit([c.text for c in chunks])
    index = InMemoryIndex()
    for chunk in chunks:
        index.add(chunk.chunk_id, embedder.embed(chunk.text))
    log.info("policy corpus indexed: %s chunks from %s", len(chunks), corpus_dir())
    return index, embedder, {c.chunk_id: c.text for c in chunks}


def _index():
    """The process-wide index, built on first use. None when there is no usable corpus."""
    global _index_state
    if _index_state is not None:
        return _index_state
    with _lock:
        if _index_state is None:
            _index_state = _build_index()
        return _index_state


def search(query: str) -> PolicyAnswer:
    """Best corpus match for `query`, or an abstention.

    The query is model-supplied free text and stays in this process: it is never logged
    (it could carry whatever the model put in it) and never returned to the model.
    """
    threshold = config.policy_retrieval_min_score()
    if threshold is None:
        log.warning(
            "policy retrieval disabled: POLICY_RETRIEVAL_MIN_SCORE is unset or unusable "
            "— every query abstains (ADR 0019)"
        )
        return abstain(NO_THRESHOLD)
    if not isinstance(query, str) or not query.strip():
        return abstain(EMPTY_QUERY)
    if _HARNESS_IMPORT_ERROR is not None:
        log.warning(
            "policy retrieval disabled: rag_eval is not importable (%s) — every query "
            "abstains (ADR 0019)",
            _HARNESS_IMPORT_ERROR,
        )
        return abstain(HARNESS_UNAVAILABLE)
    state = _index()
    if state is None:
        log.warning(
            "policy retrieval disabled: no usable corpus under %s", corpus_dir()
        )
        return abstain(NO_CORPUS)
    index, embedder, texts = state
    hits = index.search(embedder.embed(query), k=1)
    if not hits:
        return abstain(NO_CORPUS)
    chunk_id, score = hits[0]
    if score < threshold:
        log.info(
            "policy retrieval abstained: best match %s scored %.4f, below %.4f",
            chunk_id,
            score,
            threshold,
        )
        return abstain(BELOW_THRESHOLD, score=score)
    return PolicyAnswer(
        status=_HIT, score=score, chunk_id=chunk_id, text=texts[chunk_id]
    )
