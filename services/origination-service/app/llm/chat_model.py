"""LangChain chat-model seam over `ClaudeClient` (freeze plan slice 2).

The freeze requires an agent framework to own the officer loop. The framework's own
model clients talk to the provider directly, which would route around every control in
this package: the retry policy, `validate_structured`, the output guards, and — the one
that matters most — `request_builder`'s redaction of history and declared JSON
variables. `redaction-tests` and `redactor-drift` block on that behaviour, so a vendor
client would mean re-proving both against a path they were never written for.

So the framework calls DOWN into `ClaudeClient` instead of past it. This class is the
only new code in that arrangement, and it is deliberately thin: it translates a
framework message list into the exact `complete()` call the hand-rolled loop already
makes, and translates the validated result back into a framework message.

Two translation rules do the real work, and both exist to keep the redaction boundary
on the same code path rather than beside it:

1. **Every history turn is serialized into the protocol shapes the loop already
   emits**, not into a generic rendering of the framework's message. Two rules force
   this. `_redacted_turn` fails closed on a turn whose content is not a JSON object, so
   a framework `AIMessage` — whose content is prose or an empty string — cannot be
   passed through as-is. And `_SAFE_CATEGORICAL` *is* the assistant protocol vocabulary
   (`action`, `tool`, `task`, `outcome`, `policy_band`, `status`, `reason_codes`, plus
   numbers, which pass structurally): any other string value in a turn is masked
   wholesale as free text. So a turn carrying the model's prose arrives as
   `{"text": "•••• (free text redacted)"}` — token cost with no signal. The model's
   prose is therefore not carried at all, which is exactly what the hand-rolled loop
   does today, and it means this seam needs no change to the allowlist.
2. **The system prompt comes from the registered template, never from the framework.**
   `decision_assistant`'s authored system prompt carries the ADR 0009 §3 adverse-action
   reason vocabulary, and it carries it *because* tool results deliberately return only
   codes — enum codes and numbers are the only strings `_SAFE_CATEGORICAL` admits out of
   history. A framework-supplied system prompt would silently drop that vocabulary while
   the loop kept relying on it.

Not implemented here, on purpose: `bind_tools`. `CompletionRequest` carries no tool
schemas and no tool-use content blocks, so binding tools would accept them and send a
request that constrains nothing — a path reporting success for work it never did. It
raises instead. Native tool calling is a change to `CompletionRequest`, `build_request`
and the history redaction rule; it is its own slice, with its own gate.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from .errors import LLMError

# The registered prompt this seam speaks. Fixed rather than a constructor argument:
# the template supplies the system prompt AND the output schema the result is validated
# against, so a caller choosing a different one would get a validated dict this class
# cannot translate. One caller, one prompt — a parameter here would be config for a
# single call site.
_PROMPT_NAME = "decision_assistant"

# The template's only variable, and one it declares in `json_vars`, so `build_request`
# gives it JSON-aware identity masking rather than whole-string redaction.
_REQUEST_VAR = "request_json"

ROLE_BY_MESSAGE_TYPE = {"ai": "assistant", "tool": "user", "human": "user"}

# search_policy's `query` is the only free text the model may send. The loop strips it
# before the action is replayed as history, and so does this seam: the redaction
# contract masks it anyway, but a boundary that holds only because the redactor catches
# it is one allowlist entry from leaking (assistant.py says so at its own strip).
#
# Module level, NOT a class attribute: BaseChatModel is a pydantic model, so a class
# attribute named with a leading underscore becomes a ModelPrivateAttr rather than the
# string, and `name != QUERY_TOOL` is then true for every name. That defect shipped
# into a test run here and was caught only because the outbound request was asserted
# against, not the intent.
QUERY_TOOL = "search_policy"


class MeridianChatModel(BaseChatModel):
    """A framework chat model whose every call goes through `ClaudeClient.complete`.

    Construct with the client the service already built at startup
    (`app.state.llm_client`), so provider selection, credentials, the region pin and the
    trace spans are inherited rather than reproduced.
    """

    client: Any
    """A `ClaudeClient`. Untyped to keep this module importable without a live config."""

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "meridian-claude"

    # ---- framework -> ClaudeClient ------------------------------------------------

    @classmethod
    def _turn_content(cls, message: BaseMessage) -> str:
        """One history turn, in the protocol shape the hand-rolled loop emits.

        An assistant tool call becomes `{"action": "tool", "tool": ..., "input": ...}`;
        a tool result becomes `{"tool": ..., "result": ...}`. Both are the shapes
        `run()` already appends, so the redaction allowlist applies unchanged.

        The model's prose is deliberately dropped. `_SAFE_CATEGORICAL` admits only the
        protocol enums and numbers, so prose would leave as a free-text mask: carrying it
        spends tokens to tell the model nothing. A prose-only assistant turn therefore
        becomes an empty object -- still an object, so the boundary accepts it -- rather
        than a masked placeholder or a dropped turn that would change the conversation
        the model sees.
        """
        if isinstance(message, ToolMessage):
            payload: dict[str, Any] = {}
            name = getattr(message, "name", None)
            if isinstance(name, str) and name:
                payload["tool"] = name
            payload["result"] = cls._tool_result(message.content)
            return json.dumps(payload)

        tool_calls = getattr(message, "tool_calls", None)
        if isinstance(tool_calls, list) and tool_calls:
            # One action per turn, as the protocol has always been. A model emitting
            # several in one turn is refused rather than silently reduced to the first:
            # the second call would be executed by the framework and absent from the
            # history the model then reasons over.
            if len(tool_calls) > 1:
                raise LLMError(
                    "the assistant protocol carries one action per turn; got "
                    f"{len(tool_calls)} tool calls"
                )
            call = tool_calls[0]
            if not isinstance(call, dict):
                raise LLMError("a tool call must be a mapping")
            name = call.get("name")
            if not isinstance(name, str) or not name:
                raise LLMError("a tool call must carry a non-empty string name")
            action: dict[str, Any] = {"action": "tool", "tool": name}
            args = call.get("args")
            if name != QUERY_TOOL and isinstance(args, dict):
                action["input"] = args
            return json.dumps(action)

        return json.dumps({})

    @staticmethod
    def _tool_result(content: Any) -> Any:
        """A tool result as the object the loop puts under `result`.

        Tool functions return dicts, and the framework stringifies them into a
        `ToolMessage`. Parse it back so the allowlist recurses into the real fields --
        `status`, `outcome`, `policy_band`, `reason_codes` and numbers all pass, which is
        how the loop's own turns survive redaction. A result that is not an object is
        carried as-is and masked by the ordinary rules: guessing at its shape would be a
        worse failure than a mask.
        """
        if isinstance(content, dict):
            return content
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError:
                return content
            return parsed
        return content

    @classmethod
    def _split(cls, messages: list[BaseMessage]) -> tuple[str, list[dict]]:
        """Split a framework message list into (`request_json`, history turns).

        The first human message is the officer's request and becomes the template
        variable; everything after it is history. A framework system message is dropped
        deliberately — see the module docstring.
        """
        if not isinstance(messages, list) or not messages:
            raise LLMError("a chat request needs at least one message")

        first_human = next(
            (i for i, m in enumerate(messages) if isinstance(m, HumanMessage)), None
        )
        if first_human is None:
            raise LLMError(
                "a chat request needs a human message carrying the officer's request; "
                "this seam renders it into the decision_assistant template"
            )

        request_json = messages[first_human].content
        if not isinstance(request_json, str) or not request_json.strip():
            raise LLMError(
                "the officer's request message must carry a non-empty string"
            )

        history = []
        for message in messages[first_human + 1 :]:
            role = ROLE_BY_MESSAGE_TYPE.get(message.type)
            if role is None:
                raise LLMError(f"unsupported message type {message.type!r} in history")
            history.append({"role": role, "content": cls._turn_content(message)})
        return request_json, history

    # ---- ClaudeClient -> framework ------------------------------------------------

    @staticmethod
    def _to_message(action: Any) -> AIMessage:
        """Translate one validated `decision_assistant` action into an `AIMessage`.

        A tool action becomes a framework tool call so the caller dispatches through the
        framework rather than by re-parsing text. A final action keeps its validated JSON
        object as the content: the loop validates that answer against the persisted
        decision record, and it must receive the same object the schema admitted.
        """
        if not isinstance(action, dict):
            raise LLMError(
                f"decision_assistant returned {type(action).__name__}, not an object"
            )
        kind = action.get("action")
        if kind == "tool":
            name = action.get("tool")
            if not isinstance(name, str) or not name:
                raise LLMError("a tool action must name a tool")
            raw_input = action.get("input")
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": name,
                        "args": raw_input if isinstance(raw_input, dict) else {},
                        "id": f"call_{uuid.uuid4().hex}",
                    }
                ],
            )
        if kind == "final":
            return AIMessage(content=json.dumps(action))
        raise LLMError(f"decision_assistant returned unknown action {kind!r}")

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        # `stop` is refused rather than ignored: the output schema and the guards decide
        # what a valid completion is, and a caller believing it had set a stop sequence
        # would be wrong about the request that went out.
        if stop:
            raise LLMError("stop sequences are not supported on this seam")
        request_json, history = self._split(messages)
        action = self.client.complete(
            _PROMPT_NAME,
            history=history,
            **{_REQUEST_VAR: request_json},
        )
        return ChatResult(
            generations=[ChatGeneration(message=self._to_message(action))]
        )

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "CompletionRequest carries no tool schemas and no tool-use content blocks, "
            "so binding tools here would send a request that constrains nothing. Native "
            "tool calling is a change to CompletionRequest, build_request and the "
            "history redaction rule, and it is its own slice."
        )
