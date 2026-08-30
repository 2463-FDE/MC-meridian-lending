"""Concern 2 — Model adapter, and concern 5 — Streaming.

One interface (`ModelAdapter`) hides the provider behind `complete()` and
`stream()`. Adapters are *thin*: they translate our neutral request/response
shapes to and from the provider SDK and nothing else — no retry, no validation,
no budgeting, no business logic (those live in the client's collaborators).

`ClaudeAdapter` talks to the Anthropic SDK directly; `BedrockAdapter` talks to
the same models via Amazon Bedrock. Both are imported lazily (the SDK, and
`boto3` for Bedrock, are only touched inside methods) so the rest of the
package — and the whole test suite — works without either installed.
`FakeAdapter` is the in-memory double used by tests so they spend no tokens and
never flake on the network.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator

from .errors import LLMError, LLMHTTPError, LLMTimeoutError


@dataclass
class Completion:
    """Provider-neutral completion result."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    stop_reason: str = ""
    # Tool calls the model asked for, as [{"id", "name", "input"}]. Empty on every
    # request that carried no `tools`. Surfaced here rather than left in the raw
    # response because a request that sends tool schemas and then reads only `text`
    # gets an empty string back on the exact turn the model chose a tool — a path
    # reporting success for work it never did (the objection MeridianChatModel.
    # bind_tools raises today). Request side and response side ship together.
    tool_calls: list[dict] = field(default_factory=list)


@dataclass
class CompletionRequest:
    """Provider-neutral request. The request builder produces this."""

    system: str
    # [{"role": "user"|"assistant", "content": str}] for the JSON-protocol path, or
    # `content` as a list of tool_use / tool_result blocks for native tool calling.
    # Both shapes are validated and redacted by request_builder._redacted_turn.
    messages: list[dict]
    model: str
    max_tokens: int
    temperature: float
    timeout: float
    idempotency_key: str = ""
    metadata: dict = field(default_factory=dict)
    # Tool schemas, as [{"name", "description", "input_schema"}]. Authored by us and
    # validated for shape by request_builder._validated_tools; never caller data, so
    # not redacted (same standing as `system` and the few-shot examples). Empty means
    # the request is omitted from the provider call entirely, not sent as `tools=[]`.
    tools: list[dict] = field(default_factory=list)


class ModelAdapter(ABC):
    """Provider-hiding interface. All model access goes through this."""

    @abstractmethod
    def complete(self, req: CompletionRequest) -> Completion:
        """One-shot completion. Raises LLMTimeoutError / LLMHTTPError on failure."""

    @abstractmethod
    def stream(self, req: CompletionRequest) -> Iterator[str]:
        """Yield text chunks as they arrive (concern 5).

        Deferred from the Week-1 product path (ADR 0005 revision): defined and
        implemented, but not wired into a UI until the loan-summary feature.
        """


def _translate_anthropic_error(exc: Exception) -> LLMHTTPError | LLMTimeoutError:
    """Map an `anthropic` SDK exception to our neutral error (transient vs terminal).

    Shared by `ClaudeAdapter` and `BedrockAdapter` — both go through the same
    `anthropic` SDK exception types (`AnthropicBedrock` raises the same
    `anthropic.*Error` hierarchy as `Anthropic`), just over a different transport.

    The returned error carries the STATUS CODE and nothing else from the provider.
    Both call sites raise it with `from None` — see the comment there for why the
    cause chain cannot be kept.
    """
    import anthropic

    if isinstance(exc, anthropic.APITimeoutError):
        return LLMTimeoutError("Claude API did not respond within the timeout.")
    status = getattr(exc, "status_code", None)
    if status is None:
        # Connection error, etc. — treat as transient.
        return LLMHTTPError(
            f"Claude API connection error: {type(exc).__name__}",
            status_code=0,
            retryable=True,
        )
    retryable = status == 429 or 500 <= status < 600
    return LLMHTTPError(
        f"Claude API returned HTTP {status}.", status_code=status, retryable=retryable
    )


def _tool_calls(resp) -> list[dict]:
    """Tool calls from a provider response, as [{"id", "name", "input"}].

    Reads the `tool_use` blocks and nothing else. A block whose id or name is not a
    string, or whose input is not an object, is REFUSED rather than coerced: the id
    is echoed back to the provider on the next turn and the name selects which
    in-process tool runs, so a wrong-typed value would either break the turn or pick
    a dispatch target from data we never validated. `input` defaults to `{}` only
    when the key is absent, which is what a no-argument tool call looks like.
    """
    calls = []
    for block in getattr(resp, "content", None) or []:
        if getattr(block, "type", "") != "tool_use":
            continue
        block_id = getattr(block, "id", None)
        name = getattr(block, "name", None)
        raw_input = getattr(block, "input", None)
        if raw_input is None:
            raw_input = {}
        if not isinstance(block_id, str) or not block_id:
            raise LLMError("provider returned a tool_use block with no string id")
        if not isinstance(name, str) or not name:
            raise LLMError("provider returned a tool_use block with no string name")
        if not isinstance(raw_input, dict):
            raise LLMError(
                f"provider returned tool_use {name!r} whose input is "
                f"{type(raw_input).__name__}, not an object"
            )
        calls.append({"id": block_id, "name": name, "input": raw_input})
    return calls


class _AnthropicSDKAdapter(ModelAdapter):
    """Shared `complete`/`stream` for any adapter backed by the `anthropic` SDK's
    `messages.create`/`messages.stream` (same shape for `Anthropic` and
    `AnthropicBedrock`). Subclasses provide only `_sdk_client()`.
    """

    def _sdk_client(self):  # pragma: no cover - overridden by subclasses
        raise NotImplementedError

    def complete(self, req: CompletionRequest) -> Completion:
        client = self._sdk_client()
        # `tools` is omitted, not passed empty: the SDK rejects `tools=[]`, and an
        # omitted key is also the honest description of a request that constrains
        # nothing. Every other field is unconditional because every request has one.
        #
        # `tool_choice` rides with it, and only with it. "auto" is the default and is
        # stated rather than assumed; `disable_parallel_tool_use` is the load-bearing
        # half. The assistant protocol carries ONE action per turn, so two tool_use
        # blocks in one response would have the framework execute the second while the
        # history the model then reasons over records only the first. Asking the
        # provider not to do that is cheaper than reconciling it afterwards — and
        # `client._tool_action` still refuses a multi-call turn, because a request
        # parameter is a request, not a guarantee.
        extra = (
            {
                "tools": req.tools,
                "tool_choice": {"type": "auto", "disable_parallel_tool_use": True},
            }
            if req.tools
            else {}
        )
        try:
            resp = client.messages.create(
                model=req.model,
                system=req.system,
                messages=req.messages,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                timeout=req.timeout,
                **extra,
            )
        except Exception as exc:
            # `from None`, not `from exc`. Both `complete` and `call_with_retry` are
            # @traceable, and LangSmith records the exception that leaves a traced
            # function as a formatted traceback on the span. A chained cause puts the
            # provider's own exception repr in that traceback — for a rejected key
            # that is `anthropic.BadRequestError ... Error code: 400 - {'message': ...}`,
            # i.e. a raw provider error exported to a third-party telemetry vendor.
            # `process_outputs` cannot prevent it: that hook shapes SUCCESSFUL outputs
            # only, so the error path had no control on it at all, and unlike
            # LLM_TRACE_CONTENT it was not even flag-gated.
            #
            # What is lost: the provider's message body in a local stack dump. Nothing
            # reads it today — `client.complete` logs the exception CLASS, and
            # `main.py::_run_assistant` maps the error to a 503 — and the body is
            # available on the provider's own console. The status code, which is what
            # decides retryable vs terminal, is preserved in our message.
            raise _translate_anthropic_error(exc) from None

        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        return Completion(
            text=text,
            tool_calls=_tool_calls(resp),
            input_tokens=getattr(resp.usage, "input_tokens", 0),
            output_tokens=getattr(resp.usage, "output_tokens", 0),
            model=getattr(resp, "model", req.model),
            stop_reason=getattr(resp, "stop_reason", ""),
        )

    def stream(self, req: CompletionRequest) -> Iterator[str]:
        # Refused rather than ignored. `text_stream` yields text deltas only, so a
        # streamed turn on which the model chose a tool would come back as an empty
        # string and the tool call would be dropped with no error — the caller would
        # read a successful, silent no-op. Streaming with tools needs the event
        # stream, and nothing in the product streams the agent loop (concern 5 is
        # still gated), so this fails closed instead of growing an unused path.
        if req.tools:
            raise LLMError(
                "stream() cannot carry tool schemas: the text stream drops tool_use "
                "blocks, so a tool call would be silently lost. Use complete()."
            )
        client = self._sdk_client()
        try:
            with client.messages.stream(
                model=req.model,
                system=req.system,
                messages=req.messages,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                timeout=req.timeout,
            ) as stream:
                for chunk in stream.text_stream:
                    yield chunk
        except Exception as exc:
            # Same export boundary as `complete` above, same reason.
            raise _translate_anthropic_error(exc) from None


class ClaudeAdapter(_AnthropicSDKAdapter):
    """Anthropic Claude adapter (direct API). Translation only.

    The `anthropic` SDK is imported lazily inside methods so importing this module
    (and running unit tests against `FakeAdapter`) needs no SDK and no API key.
    """

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client = None  # built lazily on first call

    def _sdk_client(self):
        if self._client is None:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=self._api_key)
            except ImportError as exc:  # pragma: no cover - env-dependent
                raise LLMHTTPError(
                    "anthropic SDK is not installed; cannot reach Claude.",
                    status_code=0,
                    retryable=False,
                ) from exc
        return self._client


class BedrockAdapter(_AnthropicSDKAdapter):
    """Claude-on-Amazon-Bedrock adapter. Translation only — same contract as
    `ClaudeAdapter`, different transport.

    Auth is never passed explicitly here: `anthropic.AnthropicBedrock()` picks up
    AWS credentials the standard way (`AWS_BEARER_TOKEN_BEDROCK`, the
    `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_REGION` triple, an SSO
    profile, or an IAM role) exactly like any other `boto3`-backed client — no
    credential ever lives on this object, so there is nothing here that needs
    `repr=False` treatment (unlike `LLMConfig.api_key`).

    Requires the `anthropic[bedrock]` extra (pulls in `boto3`); imported lazily
    so the rest of the package works without it installed.
    """

    def __init__(self, region: str | None = None):
        self._region = region
        self._client = None  # built lazily on first call

    def _sdk_client(self):
        if self._client is None:
            # Construction is inside the try because the `anthropic[bedrock]`
            # extra (boto3) is imported when AnthropicBedrock() is built, not on
            # `import anthropic`. A shipped image with plain `anthropic` but no
            # boto3 would otherwise raise a RAW ImportError here instead of our
            # typed error — the exact "missing extra" gap the review flagged.
            try:
                import anthropic

                kwargs = {"aws_region": self._region} if self._region else {}
                self._client = anthropic.AnthropicBedrock(**kwargs)
            except ImportError as exc:
                raise LLMHTTPError(
                    "anthropic[bedrock] is not installed (needs boto3); "
                    "cannot reach Claude on Bedrock. Install the "
                    "'anthropic[bedrock]' extra.",
                    status_code=0,
                    retryable=False,
                ) from exc
        return self._client


class FakeAdapter(ModelAdapter):
    """In-memory adapter for tests. No network, no tokens, no SDK.

    Configure it with a canned response, a scripted sequence of exceptions to
    raise (to drive retry/timeout paths), or a callable for custom behavior.
    """

    def __init__(
        self,
        response: str = "",
        raises: list[Exception] | None = None,
        on_complete=None,
        input_tokens: int = 10,
        output_tokens: int = 10,
        responses: list[str] | None = None,
    ):
        self.response = response
        self._responses = list(responses or [])  # scripted sequence (agent-loop tests)
        self._raises = list(raises or [])
        self._on_complete = on_complete
        self._input_tokens = input_tokens
        self._output_tokens = output_tokens
        self.calls: list[CompletionRequest] = []

    def complete(self, req: CompletionRequest) -> Completion:
        self.calls.append(req)
        if self._raises:
            raise self._raises.pop(0)
        if self._on_complete is not None:
            return self._on_complete(req)
        return Completion(
            text=self._responses.pop(0) if self._responses else self.response,
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            model=req.model,
            stop_reason="end_turn",
        )

    def stream(self, req: CompletionRequest) -> Iterator[str]:
        self.calls.append(req)
        if self._raises:
            raise self._raises.pop(0)
        # Chunk the canned response to mimic token streaming.
        for i in range(0, len(self.response), 8):
            yield self.response[i : i + 8]
