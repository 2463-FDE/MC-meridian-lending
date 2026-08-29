"""Markdown eval-report writer (spec D1.5–D1.7, D2.5).

The report never contains a raw PII value: hygiene findings carry masked
samples only (hygiene.py masks before anything reaches this module), and the
historical #6012 log line is cited by location + non-PII fields, not quoted
wholesale (the adjacent lines in that purged file held raw PAN/SSN).
"""

from __future__ import annotations

import re

from rag_eval.hygiene import FileVerdict
from rag_eval.metrics import (
    HUMAN_REVIEW,
    NOT_EVALUATED,
    PROHIBITED_STATES,
    UNMAPPED,
    UNSCORABLE_CLASS,
    Aggregate,
    K_VALUES,
    QueryEval,
    VERDICT_STATES,
)

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
    evals: list[QueryEval],
    agg: Aggregate,
    threshold: float,
    embedder_signature: str,
    corpus_signature: str,
    n_chunks: int,
    wrong_abstain: int,
    false_confident: int,
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
    if agg.n_unscorable:
        # Reported like `unmapped`: a count and a reason, never a scored row. A
        # row beside the real classes would read as coverage the officer channel
        # does not have — these cases are ambiguous across documents by design,
        # carry no single frozen anchor, and describe an ask-back the closed
        # topic enum gives no way to exercise.
        lines += [
            "",
            f"**{agg.n_unscorable} case(s) are `{UNSCORABLE_CLASS}`** — ambiguous "
            "across documents by design, so they carry no single frozen anchor "
            "and are scored on nothing. They are outside every table above and "
            "excluded from every rate, including the abstention count.",
        ]
    lines += [
        "",
        "| Question id | Expected chunk(s) | Top retrieved (score) | hit@1/3/5 | RR | Verdict | Conclusion | Summary |",
        "|-------------|-------------------|-----------------------|-----------|----|---------|------------|---------|",
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
        rr = f"{e.reciprocal_rank:.2f}"
        if not e.scorable:
            # Scored on nothing (see UNSCORABLE_CLASS), so it carries no verdict:
            # a ✗ here would read as a retrieval miss beside real failures, and
            # the zeroed hits/RR behind it are an absence, not a result.
            hits = "—"
            rr = "—"
            verdict = "*(not scored)*"
        elif e.unanswerable:
            hits = "—"
            verdict = "below threshold ✓" if e.correct else "**false-confident ✗**"
        else:
            hits = "/".join("✓" if e.hits[k] else "✗" for k in K_VALUES)
            verdict = "✓" if e.correct else "✗"
        # Enum value only — never the conclusion or summary text (S-10).
        conclusion = _VERDICT_LABEL.get(e.conclusion_verdict, e.conclusion_verdict)
        summary = _VERDICT_LABEL.get(e.summary_verdict, e.summary_verdict)
        lines.append(
            # Question identified by id, never by text: a supplied evaluation
            # question is content the client excluded from retention, and this
            # report is a file on disk. `gold_queries.json` maps id back to text
            # for whoever legitimately holds it.
            f"| {e.query_id} | {expected} | {top} | {hits} | {rr} | {verdict} "
            f"| {conclusion} | {summary} |"
        )
    lines += [
        "",
        "### Confidence threshold (calibration, DL-6)",
        "",
        # repr(), not :.4f — this is the value a reader copies into
        # POLICY_RETRIEVAL_MIN_SCORE. The abstain-always candidate can be one ULP
        # above the highest observed score (math.nextafter); rounding to 4 places
        # can round it back down onto that score, reintroducing the exact
        # false-confident hit the calibration picked the cutoff to avoid.
        f"Threshold = **{threshold!r}**. Method: over the gold set, candidate thresholds "
        "are the midpoints between adjacent distinct top-1 scores plus the two outer cutoffs "
        "(the lowest score itself, and the first value above the highest) — one candidate per "
        "behaviourally distinct cutoff, so the search is exhaustive. The chosen value minimizes "
        "classification errors (answerable tops that would wrongly abstain + unanswerable "
        "tops retrieved with false confidence), preferring the widest score gap on ties. "
        f"Cosine scores from embedder `{embedder_signature}` over a corpus of "
        f"{n_chunks} chunks are lumpy, and are not comparable across a change to "
        "either. This value belongs to exactly one pair and must be re-derived "
        "when either side of it moves:",
        "",
        f"- corpus: `{corpus_signature}` ({n_chunks} chunks)",
        f"- embedder: `{embedder_signature}`",
        "",
        f"At this threshold {wrong_abstain} answerable case(s) would wrongly abstain "
        f"and {false_confident} abstention case(s) retrieve with false confidence. "
        + (
            "No cutoff on this gold set separates the two classes any better — the "
            "value is already the minimum-error choice, so the remaining errors are a "
            "property of the corpus and the gold set, not an untuned parameter. A "
            "corpus whose sections repeat across documents (purpose, scope, roles) "
            "puts scaffolding text topically near every question, which is what makes "
            "the two score distributions overlap. Abstention has to be decided "
            "explicitly rather than inferred from rank."
            if (wrong_abstain + false_confident)
            else "The two classes separate cleanly on this gold set."
        ),
        "",
    ]
    return lines


# The seed data names two denials with no recorded reason (6012/6013), but the
# subsection below is written for one of them: its heading quotes the #6012
# question and its only log evidence is `app_id=6012`. So it opens on #6012
# alone. A #6013 question gets no section rather than #6012's, which would be
# the same false statement in a new place; parameterising heading and evidence
# by app id is unbuilt because no gold case asks it.
#
# The id is bounded by digits AND by a decimal point, because a plain substring
# search over the query text opened the whole denial narrative on "account 46012"
# and on "$6012.50". Checked against `query_id` as well as `query`: the id is the
# stable key when the same case is reworded ("why was the second denial
# refused?"), and the text is what carries the id when a gold set numbers its
# cases sequentially instead.
_DENIAL_WITH_A_WRITTEN_GAP = re.compile(r"(?<![\d.])6012(?![\d.])")


def asks_about_the_written_denial(
    query_id: str, query: str, unanswerable: bool
) -> bool:
    """Whether this gold case is the one the #6012 subsection is written about.

    Public and taking plain fields because `scripts/smoke_rag_eval.sh` asks the
    same question of the gold file, before any eval exists. Two copies of this
    predicate is how the smoke ends up asserting a section the report is right
    not to render.
    """
    # `unanswerable` is part of the key, not a detail: the subsection asserts the
    # case cannot be answered, and once ADR 0008's decision-record fields exist
    # this case is answerable while its app id is unchanged.
    return bool(
        unanswerable
        and (
            _DENIAL_WITH_A_WRITTEN_GAP.search(query_id)
            or _DENIAL_WITH_A_WRITTEN_GAP.search(query)
        )
    )


# The corpus root is absolute at runtime and relative in tests, so the match is a
# suffix — anchored on the separator, because a bare `endswith` also accepts
# `legacy_kb_dump/applications.jsonl`, a different file that every claim in the
# subsection would be wrong about.
_PAST_APPLICATIONS = "kb_dump/applications.jsonl"


def _is_past_applications(path: str) -> bool:
    return path == _PAST_APPLICATIONS or path.endswith("/" + _PAST_APPLICATIONS)


_VERDICT_LABEL = {
    "supported": "supported",
    "unsupported": "unsupported",
    "human_review": "human review (counts neither way)",
    "not_evaluated": "not evaluated",
    # The negative target's states. `avoided` is the good one, which is why it
    # cannot share a label with `supported`.
    "avoided": "avoided (did not reach the prohibited conclusion)",
    "asserted": "asserted the prohibited conclusion",
}


def _support_section(agg: Aggregate) -> list[str]:
    """The three targets, side by side and never added together.

    S-1 makes the expected conclusion and the displayed summary two frozen
    targets. One merged number would hide which half failed, so they get one
    table each and there is deliberately no combined score anywhere. The
    prohibited conclusion is a third target on its own axis, with its own states
    (`avoided`/`asserted`), because "supported" would name the opposite finding.

    Counts only. S-10's retention allowlist is case id, topic, source-section
    reference, the verdicts and one rationale line -- no conclusion text, no
    summary text, no prohibited-conclusion text, no passage.
    """
    lines = ["## Support test", ""]
    for title, stat, states in (
        ("Expected conclusion", agg.conclusion_verdicts, VERDICT_STATES),
        ("Displayed summary", agg.summary_verdicts, VERDICT_STATES),
        ("Prohibited conclusion", agg.prohibited_verdicts, PROHIBITED_STATES),
    ):
        lines += [f"### {title}", "", "| Verdict | Cases |", "|---------|-------|"]
        for state in states:
            n = stat.counts.get(state, 0)
            if n:
                lines.append(f"| {_VERDICT_LABEL.get(state, state)} | {n} |")
        rate_str = f"{stat.rate:.2f}" if stat.rate is not None else "n/a"
        n_human_review = stat.counts.get(HUMAN_REVIEW, 0)
        # `states[0]` is the numerator for this target, so the printed count and
        # the printed rate cannot disagree about which state is the good one.
        lines += [
            "",
            f"Graded rate: **{rate_str}** ({stat.counts.get(states[0], 0)} of "
            f"{stat.n_graded} graded, as `{states[0]}`). {n_human_review} case(s) "
            "sent to human review count in neither the numerator nor the "
            "denominator (S-9).",
            "",
        ]
        if stat.counts.get(NOT_EVALUATED):
            lines += [
                f"{stat.counts[NOT_EVALUATED]} case(s) are not evaluated: no "
                "mechanical check applies and the evaluator did not run on this "
                "pass. They are neither a pass nor a failure, and must not be "
                "read as either.",
                "",
            ]
    lines += _verdicts_by_topic_table(agg)
    return lines


def _rate_cell(stat) -> str:
    """One target's rate for one topic, with the denominator it was taken over.

    A bare `0.00` and a bare `n/a` look alike in a table and mean opposite
    things -- nothing graded versus everything graded wrong -- so the graded
    count travels with the rate rather than sitting in a separate column.
    """
    if stat.rate is None:
        return "n/a (0 graded)"
    return f"{stat.rate:.2f} ({stat.n_graded} graded)"


def _verdicts_by_topic_table(agg: Aggregate) -> list[str]:
    """The three targets again, split per officer topic (S-4).

    S-4 keeps all eight topics in scope and asks for the report **by topic, not
    pooled**. Three columns, never one: the same S-1 rule that forbids a merged
    support number forbids a merged per-topic one, and the prohibited column
    carries the opposite polarity from the two beside it -- its rate is
    `avoided`, so high is good there for a different reason.

    `unmapped` is excluded from the table and reported beneath it as a count,
    the same treatment the retrieval table gives it: a row beside real topics
    would read as coverage the closed officer vocabulary does not have.
    """
    mapped = {n: v for n, v in agg.verdicts_by_topic.items() if n != UNMAPPED}
    if not mapped:
        return []
    lines = [
        "### By topic",
        "",
        "Rates are per target and never combined. The prohibited column scores "
        "`avoided`, not `supported` -- a high number there means the run stayed "
        "off the conclusion she prohibited.",
        "",
        "| Topic | Cases | Expected conclusion | Displayed summary | Prohibited |",
        "|-------|-------|---------------------|-------------------|------------|",
    ]
    for name, v in mapped.items():
        lines.append(
            f"| `{name}` | {v.n} | {_rate_cell(v.conclusion)} | "
            f"{_rate_cell(v.summary)} | {_rate_cell(v.prohibited)} |"
        )
    unmapped = agg.verdicts_by_topic.get(UNMAPPED)
    if unmapped:
        lines += [
            "",
            f"**{unmapped.n} case(s) carry `unmapped`** and are outside the table "
            "above. No code in the closed officer vocabulary expresses them, so "
            "they cannot be asked through the product; their verdicts are counted "
            "in the pooled totals above but score no topic.",
        ]
    lines.append("")
    return lines


def _rationale_section(evals: list[QueryEval]) -> list[str]:
    """The evaluator's one line per case, for the cases that have one.

    S-10's retention allowlist admits case id, topic, source-section reference,
    the verdicts and one rationale line. Nothing produces a rationale until the
    evaluator lands, so this section is omitted entirely rather than printing a
    column of blanks that would read as an evaluator that ran and said nothing.
    """
    rated = [e for e in evals if e.rationale]
    if not rated:
        return []
    lines = [
        "## Evaluator rationales",
        "",
        "One line per graded case (S-8, S-10). Case id and topic only -- no "
        "conclusion text, no summary text, no passage.",
        "",
        "| Case | Topic | Rationale |",
        "|------|-------|-----------|",
    ]
    for e in rated:
        rationale = e.rationale.replace("\\", "\\\\").replace("|", "\\|")
        lines.append(f"| `{e.query_id}` | `{e.topic}` | {rationale} |")
    lines.append("")
    return lines


def _data_gaps_section(
    evals: list[QueryEval],
    verdicts: list[FileVerdict],
    display_names: dict[str, str] | None = None,
) -> list[str]:
    # Every subsection here is gated on the run it describes. Both were written
    # for the run this harness started on and were emitted verbatim afterwards,
    # so on a corpus that asks neither question the report explained a denial
    # nobody asked about and asserted a hygiene refusal that never happened.
    display_names = display_names or {}
    lines: list[str] = []
    if any(
        asks_about_the_written_denial(e.query_id, e.query, e.unanswerable)
        for e in evals
    ):
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
        ]
    refused_applications = next(
        (v for v in verdicts if not v.passed and _is_past_applications(v.path)),
        None,
    )
    if refused_applications is not None:
        # Named through `display_names` and counted from the verdict, for the same
        # reason `run.py` prints refusals that way: the filename may be the
        # identifier under manifest admission, and the finding breakdown is a fact
        # about this run. The previous wording ("SSN/PAN/DOB in five of six
        # records, raw EIN in the sixth") describes one fixture and no other.
        name = display_names.get(refused_applications.path, refused_applications.path)
        counts = refused_applications.counts()
        count_str = ", ".join(f"{t}: {n}" for t, n in sorted(counts.items())) or "—"
        lines += [
            "### Past applications contribute nothing to retrieval",
            "",
            f"`{name}` was refused by the hygiene gate ({count_str}) and carries no "
            "answer content anyway — outcome without reason. Per ADR 0007, past "
            "decisions enter the corpus only as an identifier-free projection after "
            'ADR 0008\'s fields exist. The "past decisions" half of the helper ask is '
            "blocked on the data model, not on retrieval engineering.",
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
                f"{top_score:.3f}, above the calibrated threshold on a case whose "
                "expected outcome is abstention. The retrieved chunk is topically near "
                "the question without answering it, so a helper reading score alone "
                "would return plausible-but-wrong text with apparent confidence. "
                "Answerability has to be decided explicitly, not inferred from rank. "
                "This note is about the retrieval, not about why this particular "
                "case has no answer."
            )
        lines.append("")
    # A bare "## Data gaps" heading over nothing is its own false claim.
    return (["## Data gaps", ""] + lines) if lines else []


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
    corpus_signature: str,
    wrong_abstain: int,
    false_confident: int,
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
        f"- Calibrated confidence threshold: {threshold!r}",
        "",
    ]
    lines += _hygiene_section(verdicts, display_names)
    lines += _metrics_section(
        evals,
        agg,
        threshold,
        embedder_signature,
        corpus_signature,
        n_chunks,
        wrong_abstain,
        false_confident,
    )
    lines += _support_section(agg)
    lines += _rationale_section(evals)
    lines += _data_gaps_section(evals, verdicts, display_names)
    return "\n".join(lines)
