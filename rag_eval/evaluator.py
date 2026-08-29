"""The offline evaluator: three graded verdicts and one rationale line per case.

S-5 authorises an offline evaluator to read policy text as a narrow exception for
this exercise; the officer-facing path is untouched and the report says so. This
module is what that exception buys, and every control in it exists because the
client named it:

- **S-7, three separate controls.** One bounded pass (a case is graded once and a
  second attempt is refused), one retry for **technical** failure only (a bad
  *result* is never retried), and no model fallback (one model id, no alternate).
- **S-8.** It grades and nothing else. There is no path here that edits a
  conclusion, a summary or a decision -- the return value is verdicts plus a line.
- **S-9.** Anything that cannot be judged becomes `human_review`, which counts in
  neither numerator nor denominator. Nothing here defaults to a graded state.
- **S-10.** Prompts, passages and provider responses persist nowhere. The provider
  error path drops the provider's own text, the same posture
  `EmbeddingProviderError` takes and for the same reason.
- **C-6.** `rag_eval/` already ships inside the origination image, so living in
  this package does not by itself keep the judge off the product path. This module
  imports nothing from `services/` and reads no credential at import time.

`boto3` is imported lazily, so the default TF-IDF path and the blocking gate stay
stdlib-only and keyless. A client can be injected, so no test spends a real call.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from rag_eval.metrics import (
    ASSERTED,
    AVOIDED,
    HUMAN_REVIEW,
    SUPPORTED,
    UNSUPPORTED,
)

# The states an evaluator REPLY may claim for itself. Narrower than
# VERDICT_STATES/PROHIBITED_STATES on purpose: NOT_EVALUATED is the absence of a
# grade, set by the harness before any call and moved only by a check that
# actually ran (S-9) -- it is not a verdict a model can hand back. Accepting it
# here would let a provider reply of "not_evaluated" read as "the evaluator did
# not run" instead of the graded-but-inconclusive case it actually is, and S-7's
# one bounded pass means that case can never be re-graded to tell the two apart.
_REPLY_VERDICT_STATES = (SUPPORTED, UNSUPPORTED, HUMAN_REVIEW)
_REPLY_PROHIBITED_STATES = (AVOIDED, ASSERTED, HUMAN_REVIEW)

# The client's approved generator, pinned to a dated version. Not an alias: S-7
# gives one bounded pass, and a model that moves under the run cannot be reported,
# while the report has to state model ids. This is the ONLY model id in the module
# -- a second one would be the fallback S-7 forbids.
DEFAULT_JUDGE_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

# Bedrock's wrapper version for the Anthropic messages body. Not the model version.
_ANTHROPIC_VERSION = "bedrock-2023-05-31"

# Enough for three enums and one line, and no more: the reply is a small fixed
# JSON object, so a large ceiling would only buy room for prose S-10 excludes.
_MAX_TOKENS = 512

# One retry, and only for a technical failure (S-7). Counted as attempts including
# the first, the way the embedder declares its own ceiling, so "2" reads as two
# calls rather than two retries. botocore's internal retrying is disabled here:
# the client's limit is on attempts made against her account, and a retry the SDK
# makes silently is still a call she pays for and the report cannot account for.
_MAX_ATTEMPTS = 2
_BEDROCK_CLIENT_CONFIG = {"retries": {"total_max_attempts": 1, "mode": "standard"}}

_SYSTEM_PROMPT = """\
You grade one already-completed policy-retrieval case. You do not answer the \
question, rewrite anything, or decide anything: you return verdicts only.

You are given the passages the run actually retrieved, the officer's question, \
and three frozen targets. Grade each target against the retrieved passages only. \
If the passages do not settle a target, say so rather than guessing -- an \
uncertain verdict is expected and is counted in neither direction.

Return ONE JSON object and nothing else, with exactly these four keys:

- "conclusion_verdict": does the expected conclusion follow from the retrieved \
passages? One of: "supported", "unsupported", "human_review".
- "summary_verdict": is the displayed summary supported by the retrieved \
passages? One of: "supported", "unsupported", "human_review".
- "prohibited_verdict": did the retrieved passages lead AWAY from the prohibited \
conclusion? This axis has its OWN words and the opposite polarity from the two \
above -- "avoided" is the good outcome, "asserted" is the bad one. Never answer \
"supported" or "unsupported" here. One of: "avoided", "asserted", "human_review".
- "rationale": one short sentence, under 200 characters, saying why. Do not quote \
the passages. Do not restate the conclusion or the summary.
"""


@dataclass(frozen=True)
class GradingCase:
    """One case as the evaluator sees it. Built by the caller, never by this module.

    `passages` are the chunks the run actually retrieved, not the expected ones:
    a conclusion that is true of a chunk the run never surfaced is not supported
    BY THE RUN, which is what the support test asks.
    """

    query_id: str
    question: str
    passages: list[str]
    expected_conclusion: str
    displayed_summary: str
    prohibited_conclusion: str


@dataclass(frozen=True)
class CaseVerdicts:
    """What the evaluator returns. Verdicts and one line -- nothing else (S-8)."""

    conclusion: str
    summary: str
    prohibited: str
    rationale: str


class EvaluatorProviderError(RuntimeError):
    """A grading call failed, with the provider's own text deliberately dropped.

    A provider body can carry a request id, an account identifier, or an echo of
    the submitted passage, and the LangSmith hardening in origination-service
    explicitly does not hide errors -- so an unwrapped message is an export path,
    not just a log line. Same reasoning as `EmbeddingProviderError`.
    """


def _state_or_human_review(value, allowed: tuple[str, ...]) -> str:
    """Map a returned verdict onto its own axis, or to `human_review`.

    Each axis is checked against ITS OWN state tuple, so `supported` on the
    prohibited axis is refused rather than recorded: it would read as the
    opposite of the finding. An unknown value never defaults to a graded state
    (S-9) -- scoring "we could not tell" as "wrong" is what she ruled out.
    """
    return value if isinstance(value, str) and value in allowed else HUMAN_REVIEW


class BedrockEvaluator:
    """Grades one case per call against Amazon Bedrock, under S-7's three controls.

    Auth is whatever botocore resolves -- a Bedrock-scoped API key in
    `AWS_BEARER_TOKEN_BEDROCK`, or ordinary AWS credentials. Nothing is read here,
    at import time or otherwise, so importing this module inside the origination
    image touches no credential.
    """

    def __init__(
        self,
        *,
        region: str,
        model_id: str = DEFAULT_JUDGE_MODEL,
        client=None,
    ) -> None:
        # LangSmith stays off for the judge, by decision. `refuse_traced_provider_run`
        # in run.py reads IS_PROVIDER_BACKED off the EMBEDDER and cannot see this
        # client, so the refusal is repeated here rather than assumed. Checked
        # before anything else: a refusal after a call has already gone out is not
        # a control.
        if os.getenv("LANGSMITH_TRACING", "").strip().lower() in {"1", "true", "yes"}:
            raise ValueError(
                "LANGSMITH_TRACING is enabled and the evaluator is provider-backed "
                "— trace error bodies are not hidden, so unset it for the graded run"
            )
        # Same refusal the embedder makes, for the same reason: `region_name=None`
        # lets botocore discover one from ambient host config, and Bedrock model
        # access is granted per region, so a discovered region silently changes
        # which account grant the run depends on. Region probing is not approved.
        if not (region or "").strip():
            raise ValueError(
                "BedrockEvaluator needs an explicit region -- set AWS_REGION to "
                "the region your Bedrock model access is granted in."
            )
        self.model_id = model_id
        self.region = region
        self.calls = 0
        self.retries = 0
        # S-7 control 1. One bounded pass means one sample per case, so the set of
        # spent cases is state the evaluator has to keep -- a caller that loops, or
        # a retry written at the wrong level, would otherwise spend a second.
        self._graded: set[str] = set()
        if client is not None:
            self._client = client
        else:
            import boto3  # lazy: only when a real backend is actually used
            from botocore.config import Config

            self._client = boto3.client(
                "bedrock-runtime",
                region_name=region,
                config=Config(**_BEDROCK_CLIENT_CONFIG),
            )

    def _body(self, case: GradingCase) -> str:
        passages = "\n\n".join(
            f"[passage {i + 1}]\n{text}" for i, text in enumerate(case.passages)
        )
        user = (
            f"Officer question:\n{case.question}\n\n"
            f"Retrieved passages:\n{passages}\n\n"
            f"Expected conclusion:\n{case.expected_conclusion}\n\n"
            f"Displayed summary:\n{case.displayed_summary}\n\n"
            f"Prohibited conclusion (the run must NOT reach this):\n"
            f"{case.prohibited_conclusion}"
        )
        return json.dumps(
            {
                "anthropic_version": _ANTHROPIC_VERSION,
                "max_tokens": _MAX_TOKENS,
                # Haiku 4.5 still accepts sampling parameters (the 4.6+ models
                # reject them). It does not make a generative judge deterministic,
                # but S-7 allows one sample and this removes the cheapest source
                # of variance between one run and the next.
                "temperature": 0,
                "system": _SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user}],
            }
        )

    def _invoke(self, case: GradingCase) -> str:
        """One call, retried at most once and only for a technical failure."""
        body = self._body(case)
        last: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            self.calls += 1
            try:
                response = self._client.invoke_model(modelId=self.model_id, body=body)
                return json.loads(response["body"].read())["content"][0]["text"]
            except Exception as exc:
                # Reaching the provider at all is the technical half; a reply that
                # arrives and is merely wrong never lands here, because parsing
                # happens in grade(). S-7 allows no retry for a bad result.
                last = exc
                if attempt + 1 < _MAX_ATTEMPTS:
                    self.retries += 1
        raise EvaluatorProviderError(
            f"grading call failed after {_MAX_ATTEMPTS} attempt(s) "
            f"({type(last).__name__}) — provider text withheld"
        ) from None

    def grade(self, case: GradingCase) -> CaseVerdicts:
        if case.query_id in self._graded:
            raise RuntimeError(
                f"case {case.query_id!r} was already graded — S-7 allows one "
                "bounded pass, and a second call would spend another sample"
            )
        # Marked before the call, not after: a failed attempt still spent the case,
        # and letting a caller retry around an exception is the same second sample
        # by another route.
        self._graded.add(case.query_id)
        text = self._invoke(case)
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                raise ValueError("reply is not a JSON object")
        except (ValueError, TypeError):
            # A bad result, not a technical failure: no retry (S-7), and every
            # axis goes to human review rather than to a graded state (S-9).
            return CaseVerdicts(
                conclusion=HUMAN_REVIEW,
                summary=HUMAN_REVIEW,
                prohibited=HUMAN_REVIEW,
                rationale="evaluator reply could not be parsed",
            )
        rationale = payload.get("rationale")
        return CaseVerdicts(
            conclusion=_state_or_human_review(
                payload.get("conclusion_verdict"), _REPLY_VERDICT_STATES
            ),
            summary=_state_or_human_review(
                payload.get("summary_verdict"), _REPLY_VERDICT_STATES
            ),
            prohibited=_state_or_human_review(
                payload.get("prohibited_verdict"), _REPLY_PROHIBITED_STATES
            ),
            # One line (S-10). The length cap lives on the receiving field so
            # there is one limit rather than two that can drift.
            rationale=" ".join(rationale.split()) if isinstance(rationale, str) else "",
        )
