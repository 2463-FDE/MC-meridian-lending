"""Native tool calling, request side (freeze slice 4a).

Covers the three things that ship together here: tool schemas travelling through
`build_request` onto `CompletionRequest`, the provider adapter sending them and
surfacing `tool_use` blocks back, and the `_redacted_turn` rule for tool_use /
tool_result content blocks.

Every assertion about the boundary is made against the OUTBOUND request or the
outbound provider kwargs, never against the seam's intent. Slice 2 shipped a
query-strip that silently stopped matching (a pydantic `ModelPrivateAttr` where a
string was expected) and only an outbound-request assertion caught it.

No `create_agent`, no loop change and no `langchain` pin are in scope here: the
response side of the loop is slice 4b.
"""

import json
import types

import pytest

from app.llm.adapter import (
    ClaudeAdapter,
    Completion,
    CompletionRequest,
    FakeAdapter,
)
from app.llm.errors import LLMError, TokenBudgetExceeded
from app.llm.request_builder import build_request, estimate_tokens
from app.llm import transport
from app.prompts import get_prompt

SEARCH_POLICY = {
    "name": "search_policy",
    "description": "Look up Meridian's written lending policy.",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    },
}
SCORE = {
    "name": "score_application",
    "description": "Score an application and persist the decision record.",
    "input_schema": {
        "type": "object",
        "properties": {"application_id": {"type": "integer"}},
    },
}


def _built(**over):
    """`build_request` for the loan-summary template, with slice-4 overrides."""
    kwargs = dict(
        model="m",
        max_tokens=256,
        temperature=0.0,
        timeout=1.0,
        token_budget=20_000,
        application_json="{}",
    )
    kwargs.update(over)
    return build_request(get_prompt("loan_application_summary"), **kwargs)


# --- schemas onto the request -------------------------------------------------


def test_tool_schemas_reach_the_request_verbatim():
    """The authored schema is what goes out — not a re-derived copy."""
    req = _built(tools=[SEARCH_POLICY, SCORE]).request
    assert req.tools == [SEARCH_POLICY, SCORE]


def test_no_tools_is_an_empty_list_not_none():
    """`tools=[]` means "omit from the provider call"; the adapter reads a list."""
    assert _built().request.tools == []


def test_tool_schemas_count_against_the_token_budget():
    """Schemas are sent on every turn and are not trimmable, so they must be in
    the arithmetic. Without this a request is admitted here and rejected by the
    provider on context length."""
    fat = {
        "name": "search_policy",
        "description": "x" * 8_000,
        "input_schema": {"type": "object"},
    }
    with pytest.raises(TokenBudgetExceeded):
        _built(tools=[fat], token_budget=1_200)


@pytest.mark.parametrize(
    "tools",
    [
        {
            "name": "t",
            "description": "d",
            "input_schema": {"type": "object"},
        },  # not a list
        ["search_policy"],  # not an object
        [{"description": "d", "input_schema": {"type": "object"}}],  # no name
        [
            {
                "name": "search policy",
                "description": "d",
                "input_schema": {"type": "object"},
            }
        ],
        # non-ASCII passes _is_field_name (str.isalpha()) but not the provider's
        # tool-name contract (^[a-zA-Z0-9_-]{1,64}$) — would be rejected on the
        # wire instead of before it
        [
            {
                "name": "búscar_política",
                "description": "d",
                "input_schema": {"type": "object"},
            }
        ],
        # over the provider's 64-char cap
        [
            {
                "name": "t" * 65,
                "description": "d",
                "input_schema": {"type": "object"},
            }
        ],
        [{"name": "t", "description": "  ", "input_schema": {"type": "object"}}],
        [{"name": "t", "description": "d"}],  # no input_schema
        [{"name": "t", "description": "d", "input_schema": "object"}],
        [{"name": "t", "description": "d", "input_schema": {"type": "string"}}],
        [{"name": "t", "description": "d", "input_schema": {"type": "object"}, "x": 1}],
    ],
)
def test_malformed_tool_schema_is_refused(tools):
    """Tool schemas are NOT redacted, on the standing that they are authored by us.
    That holds only while the shape is a strict allowlist, so anything else fails
    closed before the network."""
    with pytest.raises(LLMError):
        _built(tools=tools)


def test_duplicate_tool_name_is_refused():
    """The provider would bind one schema and the loop would dispatch on the
    other."""
    with pytest.raises(LLMError):
        _built(tools=[SEARCH_POLICY, dict(SEARCH_POLICY)])


# --- the adapter -------------------------------------------------------------


class _FakeSDK:
    """Records the kwargs `messages.create` was called with, and answers with
    whatever content blocks the test scripted."""

    def __init__(self, blocks=()):
        self.calls = []
        self._blocks = list(blocks)
        self.messages = types.SimpleNamespace(create=self._create, stream=self._stream)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return types.SimpleNamespace(
            content=self._blocks,
            usage=types.SimpleNamespace(input_tokens=1, output_tokens=2),
            model=kwargs["model"],
            stop_reason="tool_use" if self._blocks else "end_turn",
        )

    def _stream(self, **kwargs):  # pragma: no cover - refused before it is reached
        self.calls.append(kwargs)
        raise AssertionError("stream must be refused before the SDK is touched")


def _adapter(sdk):
    adapter = ClaudeAdapter(api_key="k")
    adapter._client = sdk  # skip the lazy SDK import
    return adapter


def _req(**over):
    base = dict(
        system="s",
        messages=[{"role": "user", "content": "hi"}],
        model="m",
        max_tokens=10,
        temperature=0.0,
        timeout=1.0,
    )
    base.update(over)
    return CompletionRequest(**base)


def test_tools_are_omitted_from_the_provider_call_when_empty():
    """An omitted key, not `tools=[]`: the SDK rejects an empty list, and omission
    is the honest description of a request that constrains nothing."""
    sdk = _FakeSDK([types.SimpleNamespace(type="text", text="ok")])
    _adapter(sdk).complete(_req())
    assert "tools" not in sdk.calls[0]


def test_tools_are_passed_to_the_provider_when_present():
    sdk = _FakeSDK([types.SimpleNamespace(type="text", text="ok")])
    _adapter(sdk).complete(_req(tools=[SEARCH_POLICY]))
    assert sdk.calls[0]["tools"] == [SEARCH_POLICY]


def test_tool_use_blocks_surface_as_tool_calls():
    """A request that sends schemas and reads only `text` gets an empty string on
    the exact turn the model chose a tool. Response side ships with request side."""
    sdk = _FakeSDK(
        [
            types.SimpleNamespace(
                type="tool_use",
                id="toolu_01ABC",
                name="search_policy",
                input={"query": "late fee"},
            )
        ]
    )
    out = _adapter(sdk).complete(_req(tools=[SEARCH_POLICY]))
    assert out.text == ""
    assert out.tool_calls == [
        {"id": "toolu_01ABC", "name": "search_policy", "input": {"query": "late fee"}}
    ]


def test_tool_use_with_no_input_key_is_a_no_argument_call():
    sdk = _FakeSDK(
        [types.SimpleNamespace(type="tool_use", id="toolu_1", name="ping", input=None)]
    )
    out = _adapter(sdk).complete(_req(tools=[SEARCH_POLICY]))
    assert out.tool_calls == [{"id": "toolu_1", "name": "ping", "input": {}}]


@pytest.mark.parametrize(
    "block",
    [
        types.SimpleNamespace(type="tool_use", id=None, name="t", input={}),
        types.SimpleNamespace(type="tool_use", id="toolu_1", name=None, input={}),
        types.SimpleNamespace(type="tool_use", id="toolu_1", name="t", input="q"),
    ],
)
def test_malformed_tool_use_from_the_provider_is_refused(block):
    """The id is echoed back on the next turn and the name selects which
    in-process tool runs; a wrong-typed value is refused, not coerced."""
    with pytest.raises(LLMError):
        _adapter(_FakeSDK([block])).complete(_req(tools=[SEARCH_POLICY]))


def test_stream_refuses_tool_schemas():
    """`text_stream` drops tool_use blocks, so a streamed tool call would be lost
    silently and the caller would read a successful no-op."""
    sdk = _FakeSDK()
    adapter = _adapter(sdk)
    with pytest.raises(LLMError):
        list(adapter.stream(_req(tools=[SEARCH_POLICY])))
    assert sdk.calls == []  # refused before the SDK is touched


def test_completion_tool_calls_default_to_empty():
    assert Completion(text="hi").tool_calls == []


# --- the content-block redaction rule ----------------------------------------

TOOL_USE_TURN = {
    "role": "assistant",
    "content": [
        {
            "type": "tool_use",
            "id": "toolu_01ABC",
            "name": "search_policy",
            "input": {"query": "what is the late fee"},
        }
    ],
}


def _sent(history):
    """The outbound history, as content blocks, for one history list."""
    messages = _built(history=history).request.messages
    return [m for m in messages if isinstance(m["content"], list)]


def test_a_tool_use_turn_reaches_the_provider_as_blocks():
    """Before slice 4a a list content raised LLMError ('must be a string'), so a
    native tool-calling turn could not cross the boundary at all."""
    sent = _sent([TOOL_USE_TURN])
    assert len(sent) == 1
    block = sent[0]["content"][0]
    assert sent[0]["role"] == "assistant"
    assert block["type"] == "tool_use"
    assert block["id"] == "toolu_01ABC"
    assert block["name"] == "search_policy"


def test_the_model_authored_query_is_masked_not_forwarded():
    """`query` is the only free text the model may send and it is NOT in
    `_SAFE_CATEGORICAL`, so it leaves as a free-text mask exactly as it does on the
    JSON-protocol path. The rule is not loosened to let it through."""
    block = _sent([TOOL_USE_TURN])[0]["content"][0]
    assert block["input"]["query"] != "what is the late fee"
    assert "late fee" not in json.dumps(block["input"])


def test_tool_result_identity_is_masked():
    """A tool result is caller/tool-supplied content and gets the same structural
    masking as any other turn."""
    turn = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_01ABC",
                "content": json.dumps(
                    {
                        "status": "recorded",
                        "name": "Jane Smith",
                        "ssn": "412-55-9981",
                        "model_score": 640,
                    }
                ),
            }
        ],
    }
    sent = json.dumps(_sent([turn]))
    for leaked in ("Jane Smith", "412-55-9981", "9981"):
        assert leaked not in sent, f"identity leaked via a tool result: {leaked!r}"
    assert "recorded" in sent  # protocol enum survives
    assert "640" in sent  # a number survives structurally


def test_tool_result_accepts_a_dict_content():
    """The framework stringifies tool returns, but an in-process caller hands the
    dict straight over. Both are objects; both are masked the same way."""
    turn = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": {"status": "policy_hit"},
                "is_error": False,
            }
        ],
    }
    block = _sent([turn])[0]["content"][0]
    assert json.loads(block["content"])["status"] == "policy_hit"
    assert block["is_error"] is False


@pytest.mark.parametrize(
    "content",
    [
        "Prior borrower Jane Smith, DOB 1970-01-01, of 10 Main St.",  # prose
        '"Jane Smith DOB 1970-01-01"',  # prose in quotes
        '["Jane Smith", "1970-01-01"]',  # array
        "42",  # bare scalar
        42,  # not even a string
    ],
)
def test_tool_result_that_is_not_an_object_is_refused(content):
    """Same fail-closed rule as a string history turn, for the same reason: an
    object is the only shape in which a label-only identifier can be masked."""
    turn = {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "toolu_1", "content": content}
        ],
    }
    with pytest.raises(LLMError):
        _built(history=[turn])


@pytest.mark.parametrize(
    "turn",
    [
        # prose block — cannot be masked, so refused rather than dropped silently
        {"role": "assistant", "content": [{"type": "text", "text": "Jane Smith"}]},
        # tool_result on the assistant turn: the assistant asks, the user answers
        {
            "role": "assistant",
            "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": {"a": 1}}
            ],
        },
        # tool_use on the user turn
        {
            "role": "user",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "t", "input": {}}
            ],
        },
        {"role": "assistant", "content": []},  # a turn that says nothing
        {"role": "assistant", "content": ["tool_use"]},  # block is not an object
        # unlisted key: neither validated nor redacted
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "t",
                    "input": {},
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        },
        # id is echoed to the provider unredacted, so its charset is constrained
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "Jane Smith", "name": "t", "input": {}}
            ],
        },
        # name selects which in-process tool runs
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "1 Main St", "input": {}}
            ],
        },
        # non-ASCII name: passes _is_field_name's str.isalpha() but not the
        # provider's tool-name contract
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "búscar_política",
                    "input": {},
                }
            ],
        },
        # over the provider's 64-char cap
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "t" * 65, "input": {}}
            ],
        },
        # input must be an object to be masked structurally
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "t", "input": "Jane"}
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": {"a": 1},
                    "is_error": "yes",
                }
            ],
        },
        # an unrecognized role is copied onto the provider message unredacted
        {
            "role": "Zenobia",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "t", "input": {}}
            ],
        },
    ],
)
def test_malformed_content_block_turn_is_refused(turn):
    with pytest.raises(LLMError):
        _built(history=[turn])


def test_a_string_turn_still_behaves_exactly_as_before():
    """Regression: the JSON-protocol path is untouched by the block rule."""
    ok = {"role": "user", "content": '{"status": "recorded"}'}
    assert "recorded" in json.dumps(_built(history=[ok]).request.messages)
    with pytest.raises(LLMError):
        _built(history=[{"role": "user", "content": "free prose"}])
    with pytest.raises(LLMError):
        _built(history=[{"role": "Zenobia", "content": '{"a": 1}'}])


# --- the budget arithmetic over block content --------------------------------


def test_block_content_is_measured_over_its_serialization():
    """`estimate_tokens` on a list would be `len()` of the list — a handful of
    tokens for a turn that is kilobytes on the wire, so history would never trim
    and the provider would reject the request on context length."""
    # Imported here, not at module scope: `make prove` rolls the SOURCE back to the
    # parent commit and leaves this file at the fix, so a module-level import of a
    # new symbol turns step 1 into a collection error — a red that proves only that
    # the code is new. Locally imported, step 1 instead fails on the behaviour the
    # block rule adds.
    from app.llm.request_builder import _content_tokens

    blocks = TOOL_USE_TURN["content"]
    assert _content_tokens(blocks) == estimate_tokens(
        json.dumps(blocks, ensure_ascii=False)
    )
    assert _content_tokens(blocks) > len(blocks)


def test_block_history_trims_to_fit_the_budget():
    """Numbers survive redaction structurally, so this measures the real outbound
    size rather than the size of a masked placeholder."""
    fat = {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": {f"model_score_{i}": 640 for i in range(200)},
            }
        ],
    }
    built = _built(history=[fat, fat, fat, fat], token_budget=4_000)
    assert built.trimmed_history_turns >= 1


@pytest.mark.parametrize("content_enabled", [False, True])
def test_the_trace_processors_export_neither_schemas_nor_tool_calls(
    monkeypatch, content_enabled
):
    """Both processors are allowlists, so the two new fields are untraced by
    default AND under LLM_TRACE_CONTENT. `tool_calls` carries the model-authored
    query pre-validation, which is the same content llm.transport already refuses
    to export from `text`; `tools` is authored but is not what the span is for."""
    monkeypatch.setattr(transport, "trace_content_enabled", lambda: content_enabled)
    traced_in = transport._trace_transport_inputs(
        {"req": _req(tools=[SEARCH_POLICY]), "max_retries": 1}
    )
    traced_out = transport._trace_transport_outputs(
        Completion(
            text="",
            tool_calls=[{"id": "toolu_1", "name": "search_policy", "input": {}}],
        )
    )
    assert "tools" not in traced_in
    assert "tool_calls" not in traced_out


def test_fake_adapter_records_the_outbound_tools():
    """The double the loop tests run against must expose `tools`, or slice 4b's
    assertions would be made against intent instead of the request."""
    adapter = FakeAdapter(response="{}")
    adapter.complete(_req(tools=[SEARCH_POLICY]))
    assert adapter.calls[0].tools == [SEARCH_POLICY]
