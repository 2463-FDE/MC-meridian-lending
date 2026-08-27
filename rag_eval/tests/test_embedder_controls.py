"""Operating controls the client set for the Titan run.

Every constraint here came from the client and is verified rather than asserted in
prose: an explicit output dimension, that dimension bound into the cache signature,
a retry ceiling of two attempts per call, no raw provider error escaping, per-call
counters for the required post-run report, and an explicitly configured region
rather than one boto3 discovers.

No test here imports boto3. `rag_eval` is deliberately stdlib-only and keyless so
the blocking gate runs in CI with no credentials, so the client is injected and the
boto3-facing configuration is asserted as data.
"""

import json

import pytest

from rag_eval.embedder import (
    _BEDROCK_CLIENT_CONFIG,
    DEFAULT_EMBED_DIMENSIONS,
    BedrockEmbedder,
    EmbeddingProviderError,
)


class FakeBody:
    def __init__(self, payload: dict):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode()


class FakeClient:
    """Captures request bodies and returns a Titan-shaped response."""

    def __init__(self, dims: int = DEFAULT_EMBED_DIMENSIONS, retry_attempts: int = 0):
        self.bodies: list[dict] = []
        self._dims = dims
        self._retry_attempts = retry_attempts

    def invoke_model(self, modelId: str, body: str):
        self.bodies.append(json.loads(body))
        return {
            "body": FakeBody(
                {
                    "embedding": [0.5] * self._dims,
                    "inputTextTokenCount": 7,
                }
            ),
            "ResponseMetadata": {"RetryAttempts": self._retry_attempts},
        }


class ExplodingClient:
    def __init__(self, message: str):
        self._message = message

    def invoke_model(self, modelId: str, body: str):
        raise RuntimeError(self._message)


def _embedder(client) -> BedrockEmbedder:
    e = BedrockEmbedder(model_id="amazon.titan-embed-text-v2:0", client=client)
    e.fit([])
    return e


def test_embed_sends_explicit_dimensions():
    # An omitted `dimensions` takes whatever the API defaults to. The client asked
    # for an exactly specified configuration, so the value is declared, not inherited.
    client = FakeClient()
    _embedder(client).embed("late payment fee")
    assert client.bodies[0]["dimensions"] == DEFAULT_EMBED_DIMENSIONS


def test_signature_binds_the_dimension():
    # Two vectors from the same model at different dimensions are not comparable.
    # The signature keys the cache, so a dimension change must invalidate it —
    # otherwise 1024-dim chunks are scored against a 256-dim query and it reads as
    # a retrieval regression rather than a configuration error.
    a = _embedder(FakeClient())
    b = BedrockEmbedder(
        model_id="amazon.titan-embed-text-v2:0", dimensions=256, client=FakeClient(256)
    )
    b.fit([])
    assert str(DEFAULT_EMBED_DIMENSIONS) in a.signature
    assert a.signature != b.signature


def test_retry_ceiling_is_two_attempts():
    # The client capped retries at two attempts per embedding call. boto3's own
    # default is higher, so the ceiling has to be declared rather than inherited.
    assert _BEDROCK_CLIENT_CONFIG["retries"]["max_attempts"] == 2
    assert _BEDROCK_CLIENT_CONFIG["retries"]["mode"] == "standard"


def test_provider_error_is_wrapped_and_scrubbed():
    # "No retention of raw provider errors." An unwrapped botocore exception
    # propagates its body into whatever logs or traces the caller has — and the
    # LangSmith hardening deliberately does NOT hide errors.
    raw = "ValidationException: request id 7f3a body {'pan': '4111111111111111'}"
    e = _embedder(ExplodingClient(raw))
    with pytest.raises(EmbeddingProviderError) as excinfo:
        e.embed("late payment fee")
    assert raw not in str(excinfo.value)
    assert "4111111111111111" not in str(excinfo.value)
    assert "embedding call failed" in str(excinfo.value)


def test_counters_record_calls_retries_and_tokens():
    # The client's post-run report requires call counts, retry counts and cost.
    # Cost derives from token count, so all three are recorded at the call site
    # rather than reconstructed afterwards.
    client = FakeClient(retry_attempts=1)
    e = _embedder(client)
    e.embed("late payment fee")
    e.embed("adverse action timing")
    assert e.calls == 2
    assert e.retries == 2
    assert e.input_tokens == 14


def test_counters_count_a_failed_call():
    # A call that failed still reached the provider and still costs an attempt
    # against the ceiling; a counter that only records successes understates both.
    e = _embedder(ExplodingClient("boom"))
    with pytest.raises(EmbeddingProviderError):
        e.embed("late payment fee")
    assert e.calls == 1


def test_dimension_mismatch_from_provider_is_refused():
    # A response whose width is not what was asked for must not silently enter the
    # index next to correctly-sized vectors.
    client = FakeClient(dims=256)
    e = _embedder(client)
    with pytest.raises(EmbeddingProviderError):
        e.embed("late payment fee")


def test_make_embedder_requires_region_for_bedrock(monkeypatch):
    # "No region probing." boto3 resolves a region itself when passed None, so the
    # check has to happen before the client is constructed — which also keeps this
    # test from needing boto3 installed.
    from rag_eval.run import make_embedder

    monkeypatch.setenv("RAG_EMBEDDER", "bedrock")
    monkeypatch.delenv("AWS_REGION", raising=False)
    with pytest.raises(ValueError, match="AWS_REGION"):
        make_embedder()


def test_make_embedder_still_refuses_an_unknown_backend(monkeypatch):
    # "No fallback model": an unrecognised value fails loud rather than quietly
    # selecting a different backend than the one asked for.
    from rag_eval.run import make_embedder

    monkeypatch.setenv("RAG_EMBEDDER", "titan-ish")
    with pytest.raises(ValueError):
        make_embedder()
