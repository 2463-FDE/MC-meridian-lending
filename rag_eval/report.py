"""Markdown eval-report writer (spec D1.5–D1.6, D2.5).

The report never contains a raw PII value: hygiene findings carry masked
samples only (hygiene.py masks before anything reaches this module), and the
historical #6012 log line is cited by location + non-PII fields, not quoted
wholesale (the adjacent lines in that purged file held raw PAN/SSN).
"""

from __future__ import annotations

from rag_eval.hygiene import FileVerdict
from rag_eval.metrics import UNMAPPED, Aggregate, K_VALUES, QueryEval

# The only trace of denial 6012's reason anywhere in the estate. The file was
# purged from the repo in the 2026-07 security remediation (commit 9ba96ee,
# docs/security-remediation-2026-07.md) because neighboring lines held raw
# PAN/SSN — which is itself evidence: logs are ephemeral, not a system of record.
_LOG_TRACE = (
    "`logs/payment-service.log:14` (purged from the repo by commit `9ba96ee`; "
    "recoverable only from git history): "
    "`GET /decision app_id=6012 model_score=612 decision=deny "
    'adverse_action_reason="purchasing history"`'
)


def _hygiene_section(
    verdicts: list[FileVerdict], display_names: dict[str, str] | None = None
) -> list[str]:
    # `display_names` maps a path to the name safe to print for it: under manifest
    # admission the filename is graded by nothing and can itself be the identifier,
    # so the report shows the digest-derived doc id instead (run.corpus_doc_id).
    display_names = display_names or {}
    lines = ["## Corpus hygiene gate (ADR 0007)", ""]
    lines.append("| File | Verdict | Findings (count per type) | Masked samples |")
    lines.append("|------|---------|---------------------------|----------------|")
    for v in verdicts:
        counts = v.counts()
        count_str = ", ".join(f"{t}: {n}" for t, n in sorted(counts.items())) or "—"
        samples = sorted({f.masked_sample for f in v.findings})[:3]
        sample_str = ", ".join(f"`{s}`" for s in samples) or "—"
        verdict = "PASS" if v.passed else "**REFUSED**"
        name = display_names.get(v.path, v.path)
        lines.append(f"| `{name}` | {verdict} | {count_str} | {sample_str} |")
    lines += [
        "",
        "Refused files are excluded wholesale — never chunked, embedded, or cached "
        "(exclusion over redaction, ADR 0007 rule 3; gate enforced in `run.py`, rule 4). "
        "Samples above are masked by the validator; raw values appear nowhere in this report.",
        "",
    ]
    return lines


def _metrics_section(
    evals: list[QueryEval], agg: Aggregate, threshold: float, embedder_signature: str
) -> list[str]:
    lines = ["## Retrieval metrics", ""]
    hit_cells = " · ".join(f"hit@{k} = {agg.hit_at_k[k]:.2f}" for k in K_VALUES)
    lines.append(
        f"Answerable queries: **{agg.n_answerable}** — {hit_cells} · MRR = {agg.mrr:.2f}. "
        f"Unanswerable queries: **{agg.n_unanswerable}**, "
        f"{agg.unanswerable_correct} correctly below threshold."
    )
    # Per officer topic, because the client asked for results by topic rather
    # than as one pooled number — and because a pooled number cannot show one
    # topic failing whole. `unmapped` gets NO row: it is not a topic, it is the
    # absence of one, and a row scoring it beside real topics reads as coverage
    # the officer channel does not have. It is reported under the table instead,
    # as a count, which is what "excluded from every per-topic score" means.
    mapped = {n: s for n, s in agg.by_topic.items() if n != UNMAPPED}
    if mapped:
        lines += [
            "",
            "| Topic | Cases | Correct |",
            "|-------|-------|---------|",
        ]
        for name, stat in mapped.items():
            lines.append(f"| `{name}` | {stat.n} | {stat.correct} |")
    if agg.n_unmapped:
        lines += [
            "",
            f"**{agg.n_unmapped} case(s) carry `unmapped`** — no code in the "
            "closed officer vocabulary expresses them, so they cannot be asked "
            "through the product. They are outside the table above and excluded "
            "from every per-topic score.",
        ]
    # Per outcome class, because an aggregate cannot show a class failing whole.
    # A corpus whose sections repeat across documents (scaffolding headings such
    # as purpose, scope, roles) can hold a healthy mean while every query of one
    # class lands on the wrong document. Counts only — no query text.
    if agg.by_class:
        lines += [
            "",
            "| Outcome class | Cases | Correct |",
            "|---------------|-------|---------|",
        ]
        for name, stat in agg.by_class.items():
            lines.append(f"| `{name}` | {stat.n} | {stat.correct} |")
    lines += [
        "",
        "| Question id | Expected chunk(s) | Top retrieved (score) | hit@1/3/5 | RR | Verdict |",
        "|-------------|-------------------|-----------------------|-----------|----|---------|",
    ]
    for e in evals:
        # Label an empty expected by what the case IS, not by what is missing:
        # only an abstention case is legitimately expectation-free, and the
        # loader now refuses a scored class without one. A dash keeps a
        # directly-constructed eval from being labelled the one class it is not.
        expected = ", ".join(f"`{c}`" for c in e.expected) or (
            "*(unanswerable)*" if e.unanswerable else "—"
        )
        top = (
            ", ".join(f"`{cid}` ({score:.3f})" for cid, score in e.retrieved[:3]) or "—"
        )
        if e.unanswerable:
            hits = "—"
            verdict = "below threshold ✓" if e.correct else "**false-confident ✗**"
        else:
            hits = "/".join("✓" if e.hits[k] else "✗" for k in K_VALUES)
            verdict = "✓" if e.correct else "✗"
        lines.append(
            # Question identified by id, never by text: a supplied evaluation
            # question is content the client excluded from retention, and this
            # report is a file on disk. `gold_queries.json` maps id back to text
            # for whoever legitimately holds it.
            f"| {e.query_id} | {expected} | {top} | {hits} | "
            f"{e.reciprocal_rank:.2f} | {verdict} |"
        )
    lines += [
        "",
        "### Confidence threshold (calibration, DL-6)",
        "",
        f"Threshold = **{threshold:.4f}**. Method: over the gold set, candidate thresholds "
        "are midpoints between adjacent distinct top-1 scores; the chosen value minimizes "
        "classification errors (answerable tops that would wrongly abstain + unanswerable "
        "tops retrieved with false confidence), preferring the widest score gap on ties. "
        f"Cosine scores from embedder `{embedder_signature}` over a ~9-chunk corpus are "
        "lumpy; this value is calibrated to this gold set and this backend, and must be "
        "re-calibrated when the corpus or embedding backend changes.",
        "",
    ]
    return lines


def _data_gaps_section(evals: list[QueryEval]) -> list[str]:
    lines = ["## Data gaps", ""]
    lines += [
        '### Why "why was application #6012 denied?" cannot be answered',
        "",
        "This is a **data-capture failure, not a retrieval bug**. The answer was never "
        "recorded anywhere retrievable:",
        "",
        "- The `decisions` table stores outcome only — `decisions(app_id, outcome)`, "
        "no reason, no drivers, no timestamp, no decider (`db/init/001_schema.sql:59`; "
        'schema comment: *"Decision: OUTCOME ONLY."*).',
        '- The seed data says it outright: *"Denials 6012/6013 have no recorded reason '
        'anywhere"* (`db/init/002_seed.sql:38`).',
        '- The underwriting guidelines flag the practice themselves: *"the tool currently '
        "records the outcome of a decision but the reasons are produced ad hoc at "
        'letter-generation time"* (`policies/underwriting_guidelines.md`, Adverse action).',
        f"- The only trace in the whole estate is one unstructured log line: {_LOG_TRACE}. "
        "It is ephemeral, non-queryable, and not a system of record — and its content is "
        'itself non-compliant: "purchasing history" is not specific Reg B principal-reason '
        "language, and `model_score=612` falls in the policy's **refer band (600–659)** per "
        "`policies/underwriting_guidelines.md` — yet the recorded outcome is deny, with no "
        "record of who overrode the band or why.",
        "",
        "**Fix path:** ADR 0008 locks the required decision-record fields (principal "
        "reasons, drivers, policy band, timestamp, decider). Backfill is impossible — "
        "reasons for 6012/6013 were never captured and no migration can recover them.",
        "",
        "### Past applications contribute nothing to retrieval",
        "",
        "`kb_dump/applications.jsonl` was refused by the hygiene gate (raw SSN/PAN/DOB in "
        "five of six records, raw EIN in the sixth) and carries no answer content anyway — "
        "outcome without reason. Per ADR 0007, past decisions enter the corpus only as an "
        'identifier-free projection after ADR 0008\'s fields exist. The "past decisions" '
        "half of the helper ask is blocked on the data model, not on retrieval engineering.",
        "",
    ]
    false_confident = [
        e for e in evals if e.unanswerable and not e.correct and e.retrieved
    ]
    if false_confident:
        lines += ["### False-confident retrievals (helper risk)", ""]
        for e in false_confident:
            top_id, top_score = e.retrieved[0]
            lines.append(
                f"- **{e.query_id}**: top hit `{top_id}` scored "
                f"{top_score:.3f}, above the calibrated threshold. The chunk describes "
                "*process/policy*, not the answer — it does not contain why this specific "
                "application was denied. A naive helper would return plausible-but-wrong "
                "text with apparent confidence. Any Week 3+ helper must detect the "
                "no-record case explicitly (e.g. answerability check against ADR 0008 "
                "decision records), not rely on retrieval score alone."
            )
        lines.append("")
    return lines


def build(
    *,
    verdicts: list[FileVerdict],
    n_chunks: int,
    display_names: dict[str, str] | None = None,
    cache_hits: int,
    cache_misses: int,
    caching: bool,
    provider_calls: int,
    provider_retries: int,
    provider_input_tokens: int,
    threshold: float,
    evals: list[QueryEval],
    agg: Aggregate,
    embedder_signature: str,
) -> str:
    refused = sum(1 for v in verdicts if not v.passed)
    # The cache counters only describe a run that had a cache. A provider backend
    # runs cacheless (rag_eval/run.py::cache_enabled), so hits and misses are both
    # structurally 0 there — printing them would report "nothing re-embedded" for
    # the graded configuration, which re-embeds every chunk and every gold query
    # on every run. Report the provider's own call counters in that mode instead.
    if caching:
        embedding_line = (
            f"- Embeddings computed this run: {cache_misses}; "
            f"served from cache: {cache_hits}"
            + (
                " — **unchanged corpus, nothing re-embedded** (spec D1.3)"
                if cache_misses == 0
                else ""
            )
        )
    else:
        embedding_line = (
            f"- Embedding calls this run: {provider_calls} "
            f"({provider_retries} retries, {provider_input_tokens} input tokens) "
            "— provider backend, no on-disk cache: every indexed chunk and every "
            "gold query is re-embedded on each run"
        )
    lines = [
        "# RAG Retrieval Eval Report (Week 2)",
        "",
        "Generated by `python -m rag_eval.run` — offline, zero LLM calls "
        "(spec D1.1). See `docs/spec-rag-week2.md`, ADR 0007, ADR 0008.",
        "",
        "## Run summary",
        "",
        f"- Files scanned by hygiene gate: {len(verdicts)} ({refused} refused)",
        f"- Chunks indexed: {n_chunks}",
        embedding_line,
        f"- Embedding backend: `{embedder_signature}`",
        f"- Calibrated confidence threshold: {threshold:.4f}",
        "",
    ]
    lines += _hygiene_section(verdicts, display_names)
    lines += _metrics_section(evals, agg, threshold, embedder_signature)
    lines += _data_gaps_section(evals)
    return "\n".join(lines)
