"""Scripted model turns that leave as PROVIDER turns, and the tests for that helper.

Five test modules drive the agent loop through `native_adapter`, so it is load-bearing
enough to test: a helper that quietly stopped emitting `tool_use` blocks would make the
whole suite pass over the text path the loop now refuses.

It lives in a `test_*.py` module rather than `conftest.py` or a bare helper module for a
mechanical reason. `scripts/prove_test.sh` treats every changed file under `tests/` as a
test file to run (`*/tests/*.py`), and pytest only collects from files matching
`python_files` — so a helper in `native_script.py` or `conftest.py` contributes a run
with no tests, which pytest exits 5 for and prove reads as a failing run. Importing a
fixture from a test module is existing practice here (`test_llm_startup.py` imports from
`test_db_readiness.py`).
"""

import json


def native_adapter(*responses, input_tokens: int = 10, output_tokens: int = 10):
    """A `FakeAdapter` whose scripted tool actions leave as provider `tool_use` blocks.

    Since the loop swap, tool calls are bound as real provider tool schemas and
    `ClaudeClient.complete` REFUSES a tool call that arrives as model text — accepting
    one is the "direct prompt-to-text call" fail condition wearing a framework's
    clothes (PR #76 round 1, B-TEXT-TOOL-FALLBACK). So a fixture that scripts
    `{"action": "tool", ...}` as a response string is no longer describing a reachable
    turn.

    Rather than rewrite every scripted case as hand-built blocks, this translates: a
    scripted TOOL action is emitted the way a provider emits one — `text=""` plus a
    `tool_use` block carrying a provider-shaped id — and everything else (the final
    answer, malformed text, prose) is emitted as text, unchanged. Tests keep asserting
    on the same scripted protocol, and what actually crosses the boundary is what a
    provider would send.

    Each block gets its own id, because the id round-trips: the `tool_result` block on
    the following turn must echo the id of the `tool_use` it answers, and a shared id
    would let a broken pairing pass.
    """
    from app.llm.adapter import Completion, FakeAdapter

    scripted = list(responses)
    issued = {"n": 0}

    def _on_complete(req):
        text = scripted.pop(0) if scripted else ""
        action = None
        if isinstance(text, str) and text.strip().startswith("{"):
            try:
                action = json.loads(text)
            except json.JSONDecodeError:
                action = None
        if isinstance(action, dict) and action.get("action") == "tool":
            issued["n"] += 1
            return Completion(
                text="",
                tool_calls=[
                    {
                        "id": f"toolu_{issued['n']:024d}",
                        "name": action.get("tool") or "",
                        "input": action.get("input") or {},
                    }
                ],
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=req.model,
                stop_reason="tool_use",
            )
        return Completion(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=req.model,
            stop_reason="end_turn",
        )

    return FakeAdapter(on_complete=_on_complete)


# --- the helper's own tests --------------------------------------------------------


def test_a_scripted_tool_action_leaves_as_a_provider_tool_use_block():
    adapter = native_adapter(
        json.dumps({"action": "tool", "tool": "search_policy", "input": {"query": "x"}})
    )
    completion = adapter.complete(_request())
    assert completion.text == ""
    assert completion.stop_reason == "tool_use"
    assert len(completion.tool_calls) == 1
    call = completion.tool_calls[0]
    assert call["name"] == "search_policy"
    assert call["input"] == {"query": "x"}
    # Provider-shaped: `request_builder._TOOL_USE_ID` only admits [A-Za-z0-9_-].
    assert call["id"].startswith("toolu_")


def test_a_scripted_final_answer_stays_text():
    body = json.dumps({"action": "final", "outcome": "deny", "reason_codes": []})
    completion = native_adapter(body).complete(_request())
    assert completion.text == body
    assert completion.tool_calls == []
    assert completion.stop_reason == "end_turn"


def test_each_turn_gets_its_own_tool_use_id():
    """The id round-trips — a shared one would let a broken pairing pass."""
    action = json.dumps({"action": "tool", "tool": "get_decision_record", "input": {}})
    adapter = native_adapter(action, action)
    first = adapter.complete(_request()).tool_calls[0]["id"]
    second = adapter.complete(_request()).tool_calls[0]["id"]
    assert first != second


def test_prose_is_carried_through_unchanged():
    """Malformed output has to reach the validator as-is, or the guard tests would be
    asserting against this helper rather than against the validator."""
    completion = native_adapter("not json at all").complete(_request())
    assert completion.text == "not json at all"
    assert completion.tool_calls == []


def _request():
    from app.llm.adapter import CompletionRequest

    return CompletionRequest(
        system="s",
        messages=[{"role": "user", "content": "{}"}],
        model="m",
        max_tokens=16,
        temperature=0.0,
        timeout=5,
    )
