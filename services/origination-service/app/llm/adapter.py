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

from .errors import LLMHTTPError, LLMTimeoutError


@dataclass
class Completion:
    """Provider-neutral completion result."""

    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    stop_reason: str = ""


@dataclass
class CompletionRequest:
    """Provider-neutral request. The request builder produces this."""

    system: str
    messages: list[dict]              # [{"role": "user"|"assistant", "content": str}]
    model: str
    max_tokens: int
    temperature: float
    timeout: float
    idempotency_key: str = ""
    metadata: dict = field(default_factory=dict)


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
    """
    import anthropic

    if isinstance(exc, anthropic.APITimeoutError):
        return LLMTimeoutError("Claude API did not respond within the timeout.")
    status = getattr(exc, "status_code", None)
    if status is None:
        # Connection error, etc. — treat as transient.
        return LLMHTTPError(f"Claude API connection error: {type(exc).__name__}",
                            status_code=0, retryable=True)
    retryable = status == 429 or 500 <= status < 600
    return LLMHTTPError(f"Claude API returned HTTP {status}.",
                        status_code=status, retryable=retryable)


class _AnthropicSDKAdapter(ModelAdapter):
    """Shared `complete`/`stream` for any adapter backed by the `anthropic` SDK's
    `messages.create`/`messages.stream` (same shape for `Anthropic` and
    `AnthropicBedrock`). Subclasses provide only `_sdk_client()`.
    """

    def _sdk_client(self):  # pragma: no cover - overridden by subclasses
        raise NotImplementedError

    def complete(self, req: CompletionRequest) -> Completion:
        client = self._sdk_client()
        try:
            resp = client.messages.create(
                model=req.model,
                system=req.system,
                messages=req.messages,
                max_tokens=req.max_tokens,
                temperature=req.temperature,
                timeout=req.timeout,
            )
        except Exception as exc:
            raise _translate_anthropic_error(exc) from exc

        text = "".join(
            block.text for block in resp.content if getattr(block, "type", "") == "text"
        )
        return Completion(
            text=text,
            input_tokens=getattr(resp.usage, "input_tokens", 0),
            output_tokens=getattr(resp.usage, "output_tokens", 0),
            model=getattr(resp, "model", req.model),
            stop_reason=getattr(resp, "stop_reason", ""),
        )

    def stream(self, req: CompletionRequest) -> Iterator[str]:
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
            raise _translate_anthropic_error(exc) from exc


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
    ):
        self.response = response
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
            text=self.response,
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
            yield self.response[i:i + 8]
