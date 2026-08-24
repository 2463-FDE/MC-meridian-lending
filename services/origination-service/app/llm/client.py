"""`ClaudeClient` — wires the seven collaborators into one call.

Flow for a structured completion:
    build request (concern 3, incl. cost guard)
      -> transport with timeout+retry (concern 4)
      -> validate + guard output (concern 6)
      -> log metrics, redacted (concern 7)

The adapter (concern 2) is injected, so tests pass `FakeAdapter` and spend no
tokens. Config (concern 1) is passed in, built via `load_llm_config()` at boot.

The client never logs the API key or raw request/response content (which carries
customer PII). It logs metrics only, and every log line additionally passes
through the service's redacting formatter as defense in depth.
"""

from __future__ import annotations

import uuid
from time import perf_counter
from typing import Any, Iterator

from langsmith import traceable
from langsmith.run_helpers import get_current_run_tree

from ..prompts import get_prompt
from .adapter import BedrockAdapter, ClaudeAdapter, ModelAdapter
from .config import LLMConfig, trace_content_enabled
from .errors import LLMConfigError, LLMError, ValidationFailed
from .logging_setup import get_llm_logger
from .request_builder import build_request
from .transport import call_with_retry
from .validator import guard_output, validate_structured

_UNSET = object()


def _trace_complete_inputs(inputs: dict) -> dict:
    """Strip prompt variables/history from the LangSmith root span.

    `complete()` receives RAW variables — redaction happens later, inside
    `build_request` — so exporting them would ship unredacted PII to a third
    party (same posture as the provider itself, ADR 0005). The traced payload
    lives on the child transport span, which only ever sees the post-redaction
    request.

    idempotency_key is NOT traced (review finding): it is caller-supplied and
    unvalidated, so a caller keying on an application number / customer reference /
    email-derived value would leak that identifier to the telemetry vendor. Omitted
    on both this span and llm.transport — see _trace_transport_inputs for the
    omit-vs-hash rationale.

    `LLM_TRACE_CONTENT` does NOT unlock this span's inputs, and that asymmetry is
    deliberate. These variables are PRE-redaction; the identical content, post-redaction,
    is available one span down on `llm.transport`, which the flag does unlock. So there is
    nothing a debugger gains here that they cannot get more safely there — only a way to
    export raw identity PII by setting one variable. The flag is for reading prompts, not
    for defeating `build_request`.
    """
    return {"prompt_name": inputs.get("prompt_name")}


def _trace_complete_outputs(output: Any) -> dict:
    """Strip the validated response body from the LangSmith root span.

    `complete()` returns the validated summary/decision (a dict or guarded
    string) — that body carries customer lending content (loan amounts, income,
    risk facts), so exporting it would ship application data to a third-party
    telemetry vendor (PR review). Trace only a non-content shape marker; token
    cost/usage already lives on the child `llm.transport` span.

    `LLM_TRACE_CONTENT=true` (non-production, see config.trace_content_enabled) adds
    `result` — this is the flag that earlier revision asked for. Unlike the raw text on
    the transport span, this body has already been through `guard_output` and
    `validate_structured`, so seeing both spans is what shows you whether a validation
    failure was the model's fault or the schema's. Off by default; never on in production.
    """
    traced = {"result_type": type(output).__name__}
    if trace_content_enabled():
        traced["result"] = output
    return traced


def _execution_mode(adapter: ModelAdapter) -> str:
    """ "real" if this adapter reaches a provider, "fixture" otherwise.

    The freeze deliverable requires the trace to state which of real/fixture/
    fallback produced an answer. This is an ALLOWLIST of the two adapters that
    actually call out, not a FakeAdapter check: an unrecognized adapter reports
    "fixture", because claiming a real provider call for something that never made
    one is the worse error of the two. "fallback" is stamped by `complete` when it
    serves one — that is a property of the call, not of the adapter.
    """
    return "real" if isinstance(adapter, (ClaudeAdapter, BedrockAdapter)) else "fixture"


def _tool_action(calls: list[dict], sent_tools: list[dict]) -> dict:
    """One provider `tool_use` block, as the action object the loop already speaks.

    Native tool calling changes what goes on the wire, not the in-process protocol:
    `assistant.run` and `MeridianChatModel._to_message` both consume
    `{"action": "tool", "tool": ..., "input": ...}`, so a provider tool call is
    translated into exactly that shape plus the provider's own `tool_use_id`. The id
    has to travel because the next turn must echo it back on a matching `tool_result`
    block, and an id we mint ourselves is one the provider never issued.

    Two refusals, both fail-closed:

    - MORE THAN ONE call in a turn. The protocol carries one action per turn, and the
      framework would execute the second while the history the model then reasons over
      showed only the first. `tool_choice.disable_parallel_tool_use` asks the provider
      not to do this; this refusal is what holds if it does it anyway.
    - A NAME WE DID NOT SEND. The name selects which in-process tool runs, so it is
      checked against the schemas this request actually carried — not against the
      prompt's enum, which describes what the model was told rather than what it was
      bound to.
    """
    if len(calls) > 1:
        raise LLMError(
            f"the provider returned {len(calls)} tool calls in one turn; the assistant "
            "protocol carries one action per turn, so a second call would run with no "
            "record of it in the history the model reasons over"
        )
    call = calls[0]
    name = call.get("name")
    bound = [t.get("name") for t in sent_tools]
    if name not in bound:
        raise LLMError(
            f"the provider asked for tool {name!r}, which this request did not bind "
            f"(bound: {sorted(n for n in bound if n)!r})"
        )
    return {
        "action": "tool",
        "tool": name,
        "input": call.get("input") or {},
        "tool_use_id": call.get("id"),
    }


def _default_adapter(config: LLMConfig) -> ModelAdapter:
    """Pick the adapter for `config.provider`. No adapter was injected."""
    if config.provider == "bedrock":
        return BedrockAdapter(region=config.aws_region)
    if config.provider == "anthropic":
        return ClaudeAdapter(config.api_key)
    raise LLMConfigError(f"unknown provider {config.provider!r}")


class ClaudeClient:
    """Hardened Claude client. Build with `ClaudeClient(load_llm_config())`."""

    def __init__(self, config: LLMConfig, adapter: ModelAdapter | None = None):
        self.config = config
        self.adapter = adapter if adapter is not None else _default_adapter(config)
        self.log = get_llm_logger()

    @traceable(
        name="llm.complete",
        process_inputs=_trace_complete_inputs,
        process_outputs=_trace_complete_outputs,
    )
    def complete(
        self,
        prompt_name: str,
        *,
        history: list[dict] | None = None,
        idempotency_key: str | None = None,
        fallback: Any = _UNSET,
        tools: list[dict] | None = None,
        **variables,
    ) -> Any:
        """Run a prompt end-to-end and return validated output.

        For a prompt with an `output_schema`, returns the parsed/validated dict.
        For a free-text prompt, returns the guarded string.

        `fallback`: if given, returned instead of raising when the model output
        fails validation/guards (never returns malformed output either way).
        Transport and budget errors always raise — a fallback would mask them.

        `tools`: authored tool schemas for native tool calling. When the provider
        answers with a tool call instead of text, this returns the action object
        `{"action": "tool", "tool": ..., "input": ..., "tool_use_id": ...}` and
        NOTHING is validated against `output_schema` — there is no text to validate.
        That is why the tool branch is taken before the validator and stamps its own
        span marker: a turn on which the schema never ran must not read as a turn it
        passed.
        """
        template = get_prompt(prompt_name)
        request_id = idempotency_key or uuid.uuid4().hex

        # Reproducibility metadata on the root span. Deliberately NOT ls_provider:
        # transport pins that to "anthropic" on both routes so LangSmith can price
        # the canonical model (see transport._LS_PROVIDER), so it cannot report
        # which route ran. Config values only — no prompt or response content.
        run_tree = get_current_run_tree()
        if run_tree is not None:
            run_tree.metadata["llm_provider"] = self.config.provider
            run_tree.metadata["execution_mode"] = _execution_mode(self.adapter)
            if self.config.aws_region:
                run_tree.metadata["aws_region"] = self.config.aws_region

        # Concern 3: build + cost guard (raises TokenBudgetExceeded before network).
        built = build_request(
            template,
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            timeout=self.config.timeout,
            token_budget=self.config.token_budget,
            history=history,
            idempotency_key=request_id,
            tools=tools,
            **variables,
        )

        # Concern 4: transport with timeout + bounded retry.
        retries = {"n": 0}

        def _on_retry(attempt, delay, exc):
            retries["n"] = attempt
            self.log.warning(
                "llm retry attempt=%d delay=%.2fs reason=%s request_id=%s",
                attempt,
                delay,
                type(exc).__name__,
                request_id,
            )

        t0 = perf_counter()
        try:
            completion = call_with_retry(
                self.adapter,
                built.request,
                max_retries=self.config.max_retries,
                on_retry=_on_retry,
            )
        except LLMError as exc:
            self.log.error(
                "llm call failed error=%s request_id=%s retries=%d",
                type(exc).__name__,
                request_id,
                retries["n"],
            )
            raise
        latency_ms = (perf_counter() - t0) * 1000

        # A native tool turn carries no text, so there is nothing for concern 6 to
        # validate — `validate_structured` on an empty string raises ValidationFailed
        # ("model output is empty") and, with a fallback in play, would stamp
        # validation_failed/fallback_used on the span for a turn that was never
        # invalid. Take the tool branch first, and mark it for what it is.
        if completion.tool_calls:
            action = _tool_action(completion.tool_calls, built.request.tools)
            run_tree = get_current_run_tree()
            if run_tree is not None:
                # The tool NAME is one of the three authored names (an enum code); the
                # model-authored `input` — search_policy's query — is not on the span.
                run_tree.metadata["tool_turn"] = True
                run_tree.metadata["tool"] = action["tool"]
            self.log.info(
                "llm tool_call request_id=%s prompt=%s v=%s model=%s latency_ms=%.0f "
                "input_tokens=%d output_tokens=%d tool=%s retries=%d",
                request_id,
                template.name,
                template.version,
                completion.model,
                latency_ms,
                completion.input_tokens,
                completion.output_tokens,
                action["tool"],
                retries["n"],
            )
            return action

        # Concern 6: validate + guard. Never pass malformed output forward.
        try:
            if template.output_schema:
                result = validate_structured(completion.text, template.output_schema)
            else:
                guard_output(completion.text)
                result = completion.text
        except ValidationFailed as exc:
            self.log.warning(
                "llm output rejected error=%s request_id=%s",
                exc,
                request_id,
            )
            if fallback is not _UNSET:
                # A served fallback must NOT read as a healthy call in LangSmith:
                # _trace_complete_outputs only sees the returned value, so without a
                # marker the root span records the fallback as a normal success and
                # repeated rejection (prompt injection, schema drift, provider
                # regression) shows green — hiding it from detection/rollback (PR
                # review). Mark the current span content-free: the exception CLASS
                # only (never str(exc), which can echo rejected model content), same
                # run-tree posture as llm.transport's ls_model_name.
                run_tree = get_current_run_tree()
                if run_tree is not None:
                    run_tree.metadata["validation_failed"] = True
                    run_tree.metadata["fallback_used"] = True
                    # Overrides the mode stamped at entry: the answer came from
                    # neither the provider nor the fixture.
                    run_tree.metadata["execution_mode"] = "fallback"
                    run_tree.metadata["rejection_error"] = type(exc).__name__
                return fallback
            raise

        # Concern 7: metrics only — no key, no raw content. (Formatter also redacts.)
        self.log.info(
            "llm ok request_id=%s prompt=%s v=%s model=%s latency_ms=%.0f "
            "input_tokens=%d output_tokens=%d est_input_tokens=%d "
            "trimmed_history=%d retries=%d",
            request_id,
            template.name,
            template.version,
            completion.model,
            latency_ms,
            completion.input_tokens,
            completion.output_tokens,
            built.estimated_input_tokens,
            built.trimmed_history_turns,
            retries["n"],
        )
        return result

    def summarize_application(self, application_json: str, **kwargs) -> dict:
        """Convenience wrapper for the loan-summary prompt.

        The application is a JSON document that must be redacted JSON-aware
        (`redact_json`) rather than by the whole-string redactor: masking a
        numeric PII literal (an SSN/PAN encoded as a JSON number) with the
        whole-string pass would emit unquoted mask text and break the JSON the
        prompt hands the model, and label-only identifiers (name/DOB/address/
        EIN/employer) carry no shape the pattern pass can key on. That
        redaction now happens in `build_request` for the prompt's declared
        `json_vars`, so EVERY caller of `complete("loan_application_summary")`
        gets it — this wrapper is a thin convenience, not the control point.
        """
        return self.complete(
            "loan_application_summary",
            application_json=application_json,
            **kwargs,
        )

    def stream(
        self, prompt_name: str, *, idempotency_key: str | None = None, **variables
    ) -> Iterator[str]:
        """Stream text chunks (concern 5). DEFERRED to Week 2 — intentionally gated.

        The adapter interface implements streaming, but the client does not yet
        wrap it with the output guards the non-streaming path enforces (schema
        validation, length, and PII-leak checks). Exposing it now would let raw,
        unvalidated model output — including any PII the model echoes — reach the
        caller, bypassing `guard_output`. Until this is implemented as
        buffer-then-validate (ADR 0005), calling it raises rather than leaking.
        """
        raise NotImplementedError(
            "streaming is deferred to Week 2 (ADR 0005): not yet buffer-then-"
            "validated, so it would bypass the output leak/schema guards. Use "
            "complete()/summarize_application() for now."
        )
        yield  # pragma: no cover - keeps this a generator for the interface
