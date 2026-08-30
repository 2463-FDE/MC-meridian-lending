"""No provider error text may cross a traced boundary (freeze privacy clause).

The trace requirement is explicit: retain no prompts, responses, queries, retrieved
text, client data, identifiers, credentials, or RAW PROVIDER ERRORS. Every other item
on that list has a control — `request_builder` redacts what goes out,
`_trace_transport_inputs` / `_trace_transport_outputs` / `_trace_complete_*` are
allowlists over what is exported, and the assistant's own spans carry enum codes only.

The error path had none. `llm.complete` and `call_with_retry` are both `@traceable`, and
LangSmith records the exception leaving a traced function as a formatted traceback. The
adapter raised our neutral error with `from exc`, so the provider's own exception repr
rode along inside it. Measured against a live LangSmith project on 2026-08-23: a 400 from
Bedrock produced a 1606-character error field on the `llm.complete` span containing
`anthropic.BadRequestError`, `Error code:` and the provider's `{'message': ...}` body.
Unlike LLM_TRACE_CONTENT that was not flag-gated — it happened on the default settings.

These tests assert the exception SURFACE, which is what LangSmith serializes: no cause,
context suppressed, and no provider marker anywhere in the formatted traceback. They run
without the `anthropic` SDK installed (a stub goes into sys.modules), because the rest of
this suite does and CI's redaction job must not be the first place this is exercised.
"""

import sys
import traceback
import types

import pytest

from app.llm.adapter import BedrockAdapter, ClaudeAdapter, CompletionRequest
from app.llm.errors import LLMHTTPError, LLMTimeoutError

# What a provider exception looks like on the wire. Every one of these strings is the
# thing that must not reach a span.
PROVIDER_BODY = (
    "Error code: 400 - {'message': 'Invalid API Key format: Must start with "
    "pre-defined prefix', 'accountId': '123456789012'}"
)
PROVIDER_MARKERS = (
    "Error code:",
    "{'message'",
    "Invalid API Key format",
    "accountId",
    "123456789012",
    "ProviderBadRequest",
)


class ProviderBadRequest(Exception):
    """Stands in for `anthropic.BadRequestError`: carries a status code and a body."""

    status_code = 400

    def __init__(self):
        super().__init__(PROVIDER_BODY)


@pytest.fixture
def anthropic_stub(monkeypatch):
    """A minimal `anthropic` module so `_translate_anthropic_error` can import it.

    The real SDK is not a test dependency here (see this suite's other files), and the
    error path cannot be reached at all without something importable — which is part of
    why this leak went unmeasured until it was seen in a live project.
    """
    stub = types.ModuleType("anthropic")
    stub.APITimeoutError = type("APITimeoutError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "anthropic", stub)
    return stub


class _RaisingSDK:
    """An SDK client whose every call raises a provider error carrying a body."""

    def __init__(self):
        self.messages = types.SimpleNamespace(create=self._raise, stream=self._raise)

    def _raise(self, **_kwargs):
        raise ProviderBadRequest()


def _req():
    return CompletionRequest(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        model="m",
        max_tokens=10,
        temperature=0.0,
        timeout=1.0,
    )


def _formatted(exc: BaseException) -> str:
    """The traceback as a serializer would render it — the exact string LangSmith
    stores on the span. `format_exception` honours `__suppress_context__`, so a
    suppressed cause is genuinely absent rather than merely unreferenced."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def _adapter_with_raising_sdk(cls, **kwargs):
    adapter = cls(**kwargs)
    adapter._client = _RaisingSDK()  # skip the lazy SDK import
    return adapter


@pytest.mark.parametrize(
    "cls,kwargs",
    [(ClaudeAdapter, {"api_key": "k"}), (BedrockAdapter, {"region": "us-east-1"})],
)
def test_complete_does_not_carry_the_provider_error_across_the_boundary(
    anthropic_stub, cls, kwargs
):
    adapter = _adapter_with_raising_sdk(cls, **kwargs)
    with pytest.raises(LLMHTTPError) as caught:
        adapter.complete(_req())
    exc = caught.value

    assert exc.__cause__ is None, "a chained cause puts the provider repr on the span"
    assert exc.__suppress_context__ is True, (
        "without suppression the serializer renders 'During handling of the above "
        "exception' plus the provider's own repr"
    )
    rendered = _formatted(exc)
    for marker in PROVIDER_MARKERS:
        assert marker not in rendered, f"provider text {marker!r} reached the span"
    # The status code survives: it is what decides retryable vs terminal, and it is
    # not provider prose.
    assert "400" in str(exc)
    assert exc.status_code == 400
    assert exc.retryable is False


@pytest.mark.parametrize(
    "cls,kwargs",
    [(ClaudeAdapter, {"api_key": "k"}), (BedrockAdapter, {"region": "us-east-1"})],
)
def test_stream_does_not_carry_the_provider_error_across_the_boundary(
    anthropic_stub, cls, kwargs
):
    """`stream()` is gated out of the product path but is still `@traceable`-adjacent
    and still raises through the same translator, so it gets the same rule."""
    adapter = _adapter_with_raising_sdk(cls, **kwargs)
    with pytest.raises(LLMHTTPError) as caught:
        list(adapter.stream(_req()))
    exc = caught.value
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True
    rendered = _formatted(exc)
    for marker in PROVIDER_MARKERS:
        assert marker not in rendered


def test_a_timeout_is_still_classified_and_still_carries_nothing(
    anthropic_stub, monkeypatch
):
    """The timeout branch reads the provider's exception TYPE, which is the one thing
    the translator legitimately needs from it. Suppressing the cause must not break
    that classification."""

    class _TimingOutSDK:
        def __init__(self):
            self.messages = types.SimpleNamespace(
                create=self._raise, stream=self._raise
            )

        def _raise(self, **_kwargs):
            raise anthropic_stub.APITimeoutError(PROVIDER_BODY)

    adapter = ClaudeAdapter(api_key="k")
    adapter._client = _TimingOutSDK()
    with pytest.raises(LLMTimeoutError) as caught:
        adapter.complete(_req())
    exc = caught.value
    assert exc.__cause__ is None
    assert PROVIDER_BODY not in _formatted(exc)


def test_a_missing_sdk_still_chains_its_import_error(monkeypatch):
    """Deliberate asymmetry, so the suppression is not read as "never chain".

    A missing `anthropic[bedrock]` extra is OUR deployment defect, and its ImportError
    message names a python module, never provider data or a credential. That chain is
    the fastest way to diagnose a broken image, so it stays.
    """
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import raises
    adapter = BedrockAdapter(region="us-east-1")
    with pytest.raises(LLMHTTPError) as caught:
        adapter.complete(_req())
    assert caught.value.__cause__ is not None
    assert isinstance(caught.value.__cause__, ImportError)
