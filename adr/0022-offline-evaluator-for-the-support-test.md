# 0022. Offline evaluator for the support test

Date: 2026-08-29

## Status

Accepted. Built in `rag_eval/evaluator.py` and wired into `rag_eval/run.py`, held
by the blocking `rag-eval-gate`. No residual: the run builds a `GradingCase` per
gold row and carries the three verdicts and the rationale onto `QueryEval`.

## Context

The client commissioned a graded evaluation of policy retrieval against a corpus
she froze and a 28-case gold set she wrote. On 2026-08-29 we settled the grading
scope: the run is graded on retrieval **plus** the support verdicts, not on
retrieval alone. Her prohibited conclusion is graded as a third axis.

Two of those three axes have nothing that can grade them. The harness already
performs a mechanical support check: where a gold row carries a `support_literal`
("30 days", "12 CFR 1002.9", "25 months", "36 percent"), the run matches that
string against the passages it actually retrieved. Four of 28 rows carry one.
A displayed summary describes a passage rather than asserting a literal, and her
prohibited conclusion is prose. So without a judgement step the report shows 24
of 28 conclusion verdicts, 28 of 28 summary verdicts and 28 of 28 prohibited
verdicts as `not_evaluated` — the deliverable measures retrieval and nothing
else, while claiming a scope that includes the support test.

Three constraints shape the answer, and all three come from the client record.

**S-7 allows one bounded pass.** One retry for technical failure only; no retry
for a bad result; no prompt tuning against seen results; no model fallback. This
is not a component we iterate against observed output. The first run is the
graded run, so every control has to be a refusal written before the run rather
than a correction made after it.

**S-5 authorises an offline evaluator to read policy text**, as a narrow
exception for this exercise, and requires that the officer-facing path stay
untouched and be reported as untouched. Her earlier packet decision
(`client-decisions/titan-embeddings-authorization.md`, 2026-08-25) lists sending
policy text to a generative language model under Not approved. She has since
named the generator herself — `us.anthropic.claude-haiku-4-5-20251001-v1:0` in
`us-east-1` — which settles the data path as Bedrock under the existing approved
AWS grant rather than an outside vendor. The report states the path explicitly so
her stop condition is visibly satisfied rather than assumed.

**S-10 is a retention allowlist**: case id, topic, source-section reference, the
verdicts, and one human-reviewed rationale line. Prompts, passages and provider
responses persist nowhere, logs and traces included.

One more constraint is structural rather than contractual. `rag_eval/` already
ships inside the origination image — `docker-compose.yml` builds that service
from the repository root, its Dockerfile copies the package, and the blocking
`rag-eval-import-gate` proves `import rag_eval` resolves in the built image,
because the `search_policy` tool depends on it. C-6 requires the evaluator to
stay off the product path, and placing it in `rag_eval/` does not achieve that on
its own.

## Decision

We will add an offline evaluator, `rag_eval/evaluator.py`, that grades one case
per call against Amazon Bedrock and returns three verdicts plus one rationale
line. It grades and does nothing else.

We will enforce each of S-7's three controls in code rather than in instructions:

1. **One bounded pass.** The evaluator records the case ids it has spent and
   refuses a second grade of the same case. It records the id before the call,
   not after, because a failed attempt still spends the case and a caller
   retrying around an exception is a second sample by another route.
2. **One retry, technical failure only.** Failing to reach the provider is
   technical and is retried once. A reply that arrives and is wrong is a result,
   never retried. We turn botocore's own retrying down to a single attempt, so a
   retry the SDK makes silently cannot spend a call the report cannot account
   for.
3. **No model fallback.** One model id exists in the module, and a test parses
   the module and asserts there is exactly one.

We will validate each axis against its own state tuple. The two support axes take
`supported` / `unsupported`; the prohibited axis takes `avoided` / `asserted`,
where `avoided` is the good outcome. A value from the wrong axis, an unknown
value, or an unparseable reply becomes `human_review`, which counts in neither
numerator nor denominator (S-9). Nothing defaults to a graded state.

We will set `temperature` to 0 and pin a dated model version.

We will refuse to construct the evaluator while `LANGSMITH_TRACING` is enabled,
and refuse a blank region.

We will keep the evaluator off the product path by assertion, not by placement:
tests parse the module and require that it imports nothing rooted at `services`
or `app`, and that nothing but definitions runs at import time.

## Options considered

**Option 1: extend the mechanical check to all 28 cases.** Give every gold row a
`support_literal` and grade every axis by string match. No model call, fully
deterministic, and it needs no authorisation at all.

Rejected. A displayed summary describes a passage instead of asserting a literal,
so the literals would have to be invented rather than taken from her data — the
gold set would then grade against a target she did not set. Loosening the match
to compensate does not help: a fuzzy match is a judgement, and it would be a
judgement made without any of the controls S-7 puts around one. The prohibited
axis is worse still, because avoiding a conclusion is not a string that appears
or fails to appear anywhere.

**Option 2: grade all 28 cases by hand.** No model, no authorisation question, no
non-determinism.

Rejected. It is 80 gradings across three axes, there is no reviewer capacity
inside the freeze window, and it answers the wrong question — the exercise asks
whether policy retrieval can be graded as a repeatable measurement, and a
one-time manual pass produces a number nobody can reproduce. Human grading stays
in the design for the `human_review` residue, which is where it is worth
spending.

**Option 3: reuse the officer assistant's existing LLM client on the product
path.** The transport, retry posture and redaction are already built and already
reviewed.

Rejected on two independent grounds. C-6 forbids it. And the officer path is the
system under test: grading it with a component that lives inside it means a
change to the assistant silently changes the measuring instrument. S-5's
exception authorises an offline evaluator, not a change to the officer channel.

**Option 4: score the axes by embedding similarity** between the expected
conclusion and the retrieved passages, reusing the Titan embedder already built.

Rejected. Similarity is not entailment. A displayed summary that contradicts a
passage on the one number that matters scores as highly similar to it, because it
shares every other word. That failure mode is exactly the one the support test
exists to detect, so a measure blind to it would report a clean run over the
defect it was commissioned to find.

**Option 5 (chosen): an offline generative evaluator in `rag_eval/`,** under
S-7's three controls, with each axis validated against its own states.

## Consequences

The graded result now depends on a component that is sampled once and is not
deterministic. Retrieval scoring, threshold calibration and the mechanical
support check remain deterministic; the evaluator is the whole non-deterministic
surface, and S-7 leaves it unhedged by design — no majority vote, no
self-consistency, no re-roll on a verdict we dislike. Temperature 0 and a pinned
model version remove the cheapest sources of variance but do not remove variance.

`human_review` becomes a bucket someone has to empty. It is the correct
destination for an uncertain verdict, and it is also where every parse failure
and every off-axis reply lands, so its size is a signal about the evaluator as
well as about the corpus.

The report gains a data path it must disclose: policy text reaches Bedrock in
`us-east-1` under the existing AWS grant. The `us.` prefix on the model id names
a cross-region inference profile, so `us-east-1` is accurate as the calling
region and would be overstated as a residency claim; the report says calling
region.

**Security.** Policy text leaves the machine for the first time in this harness.
The corpus is synthetic training material and was admitted through the hygiene
gate, which refuses labelled PII, Luhn-valid card numbers, and the rest — the
control is that the corpus was admitted clean, not that anything is stripped in
transit. The provider error path drops the provider's own text, because a body
can echo the submitted passage and the LangSmith hardening in origination-service
does not hide errors. One residual is named rather than closed:
`_UNECHOED_TEXT_KEYS` exempts the expected and prohibited conclusions from the
person-name guard on the grounds that they are never embedded and never reported,
and the evaluator now sends both to a model. That exemption is knowingly wider
than when it was written.

**Cost.** Negligible and not the binding constraint. Roughly 1.4k input tokens
per case and about 40k for a batched run, at the cheapest current model tier —
under a dollar. S-7 binds, not the budget: the scarce resource is the single
pass, so the engineering effort goes into refusals rather than into efficiency.

**Performance and scalability.** 28 sequential calls at a few seconds each. There
is no concurrency and no batching across cases, and neither is worth building for
28 cases. The mechanical exclusion does not reduce the call count: under the
conclusion-axis-only reading below, the four literal-backed rows are still graded
on summary and prohibited, so all 28 cases reach the model and only the model's
conclusion for those four is discarded. That is what the ~40k figure above already
assumes. At a corpus large enough to matter this would need revisiting, which is
the same threshold D16 tracks for pgvector.

**Reliability.** The failure modes are a provider outage (one retry, then a
raised error that names no provider text) and a malformed reply (`human_review`,
no retry). Neither can produce a false graded verdict, which is the property that
matters when there is no second pass.

**Maintainability.** The controls are tests, not comments: the bounded pass, the
retry ceiling, the absence of a fallback id, the axis polarity, the import seam
and the import-time behaviour each have one. A future change that relaxes any of
them fails `rag-eval-gate`, which blocks.

**Operational impact.** One environment variable to set
(`AWS_BEARER_TOKEN_BEDROCK`, or ordinary AWS credentials) and one to keep unset
(`LANGSMITH_TRACING`). No service, no container, no migration. The evaluator runs
from the same command as the rest of the harness.

**Testing impact.** 19 tests, all offline against an injected fake client, so the
suite stays keyless and the blocking gate can run it. No test spends a real call.

## Implementation plan

1. `rag_eval/evaluator.py` with `GradingCase`, `CaseVerdicts`, `BedrockEvaluator`
   and `EvaluatorProviderError`, plus the tests. **Done in this change.**
2. Wire it into `rag_eval/run.py`: build a `GradingCase` per gold row, skip the
   conclusion axis where `support_literal` already decided it, and assign the
   verdicts and rationale onto `QueryEval`. **Done in this change.** It also
   resolves `displayed_summary_id` against her frozen summaries package, which was
   audited but never read, and selects a backend from `RAG_JUDGE` — default
   `none`, so no dry run and no gate can spend a call by accident.
3. Run the full 28-case pass on TF-IDF first, confirming the evaluator produces
   verdicts and the report renders three rates per topic, before any Titan call.
   A retrieval-only pass on 2026-08-29 already confirmed the report side: four
   mechanical conclusions, 24 not evaluated, and all eight topics rendering three
   columns.
4. Spend the single graded pass.

## Rollback

The evaluator is additive and nothing depends on it yet. Reverting the module
returns every verdict to `not_evaluated`, which the report already renders and
already explains as neither a pass nor a failure. After step 2, rollback is the
same revert plus leaving the mechanical check in place, which keeps the four
literal-backed conclusion verdicts. No data migration, no state to unwind: the
evaluator writes nothing outside the report.

## Risks and mitigations

**An inverted verdict on the prohibited axis.** Two axes where `supported` is
good share one prompt with one where `avoided` is, and S-7 allows no retry to
correct one. Mitigated in two places: each axis is validated against its own
state tuple, so a support word on the prohibited axis becomes `human_review`
rather than being recorded as the opposite of the finding; and the prompt names
each axis by its own state words and states the polarity difference outright.
Both have tests.

**A batched prompt confusing the three axes.** Batching is what makes the third
axis free, and it is also what puts three questions in one call. Mitigated by
asking for a fixed JSON object with one named key per axis, and by treating a
reply that does not parse as `human_review` rather than as a partial result.

**Silent model drift between runs.** Mitigated by pinning a dated model version
rather than an alias, and by the report stating the model id. Not fully closed:
the `us.` inference profile can route to a different region, and we cannot
observe which one served a request.

**A second sample spent by accident.** A loop, a caller-level retry, or a retry
written at the wrong layer would each spend one. Mitigated by the spent-id set,
by recording the id before the call rather than after, and by turning botocore's
internal retrying down so the SDK cannot add a call of its own.

**The evaluator reaching the product path.** Mitigated by AST assertions rather
than by placement, because placement in `rag_eval/` is not sufficient — that
package already ships in the origination image.

**A rationale carrying passage text into the report.** S-10 admits one line. The
evaluator collapses whitespace so a paragraph cannot arrive as one; the length
cap lives on the receiving field so there is a single limit rather than two that
drift.

**The interpretation of S-6 has not been confirmed by the client.** S-6 says the
four mechanical cases must not consume a model call. We read that as the
conclusion axis only, so those four are still graded by the evaluator on the two
axes no literal covers. The whole-case reading would leave `apr_finance_charge`
with no summary verdict at all, since its single case is one of the four, and S-4
keeps all eight topics in scope. Mitigated by stating the interpretation in the
report rather than leaving it implicit. Not closed: if she reads S-6 the other
way, the affected axes for those four cases are wrong and S-7 forbids a re-run.
