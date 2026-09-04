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

A third rule closes the gap `assistant.py::run` guards and this seam's translation
alone does not: that hand-rolled loop never trusts the model's echoed `application_id`
for a tool call, redirects `score_application` to a read when `task == "explain"`, and
serves a cached result on a repeat score request so the regulated credit pull happens
at most once per run. A framework tool executor dispatches whatever `AIMessage.tool_calls`
this seam emits, so `_to_message` enforces the same three invariants before a tool call
ever leaves this class: the application id always comes from the officer's own request
(`_officer_context`, parsed from `request_json`, never from the model's `input`), a
`score_application` request is rewritten to `get_decision_record` when the task is
`explain` or a prior `ToolMessage` named `score_application` is already in history
(`_already_scored`), and only `search_policy`'s model-chosen `query` is still forwarded
from `input`.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ..prompts import get_prompt
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


def _replayed_input(name: str, args: Any, policy_topic: str | None) -> dict:
    """The `input` a replayed tool call carries back to the model.

    For every tool but `search_policy` this is the model's own arguments, which are
    protocol enums and integers the allowlist admits. For `search_policy` the
    model-authored query is stripped (above), which left the replayed block reading as
    a call with no argument at all: MEASURED against Haiku 4.5 on `task=explain` with a
    `policy_topic`, the model searched, read the record, then searched three more times
    and never answered -- its own history showed `search_policy` called with nothing, so
    the answer it had was indistinguishable from a call it had failed to make.

    The officer's `policy_topic` stands in that slot. It is a code from a closed
    vocabulary, allowlisted in `llm/request_builder._SAFE_CATEGORICAL` under this exact
    key, so it survives the boundary where the query cannot -- and it is the officer's
    own, never model-authored. Absent when the officer asked no policy question: an
    empty input is what the block carried before, and the provider pairs turns by id,
    not by input.
    """
    if name == QUERY_TOOL:
        return {"policy_topic": policy_topic} if policy_topic else {}
    return args if isinstance(args, dict) else {}


def _declared_tool_names() -> list[str]:
    """The tool names `decision_assistant`'s output schema admits.

    Read from the registered template rather than restated here: the prompt text, the
    output schema and the bound schemas have to agree, and a second copy of the list
    is a third thing to drift.
    """
    schema = get_prompt(_PROMPT_NAME).output_schema or {}
    return list(((schema.get("properties") or {}).get("tool") or {}).get("enum") or [])


def _tool_schema(tool: Any) -> dict:
    """One framework tool as an Anthropic tool schema.

    `tool_call_schema.model_json_schema()` is the framework's own rendering of the
    tool's arguments, so the schema the provider is bound to is the same object the
    framework will validate the call against — deriving it from anything else invites
    the two to disagree. Refused, not coerced, when a tool cannot produce one: a tool
    bound with no usable schema constrains nothing.
    """
    name = getattr(tool, "name", None)
    description = getattr(tool, "description", None)
    schema_model = getattr(tool, "tool_call_schema", None)
    if not isinstance(name, str) or not name:
        raise LLMError("a bound tool must carry a non-empty string name")
    if not isinstance(description, str) or not description.strip():
        raise LLMError(f"bound tool {name!r} needs a non-empty description")
    if schema_model is None or not hasattr(schema_model, "model_json_schema"):
        raise LLMError(
            f"bound tool {name!r} exposes no argument schema; a tool bound with no "
            f"schema constrains nothing"
        )
    input_schema = schema_model.model_json_schema()
    if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
        raise LLMError(
            f"bound tool {name!r} renders an argument schema of type "
            f"{input_schema.get('type')!r}; the provider sends tool input as an object"
        )
    return {"name": name, "description": description, "input_schema": input_schema}


class MeridianChatModel(BaseChatModel):
    """A framework chat model whose every call goes through `ClaudeClient.complete`.

    Construct with the client the service already built at startup
    (`app.state.llm_client`), so provider selection, credentials, the region pin and the
    trace spans are inherited rather than reproduced.
    """

    client: Any
    """A `ClaudeClient`. Untyped to keep this module importable without a live config."""

    bound_tools: list[dict] = []
    """Anthropic tool schemas this model sends, set by `bind_tools`.

    Empty means the JSON-action protocol: the model is told the wire format in the
    prompt text and its answer is validated after the fact. Non-empty means native
    tool calling — the schemas go to the provider, the provider answers with
    `tool_use` blocks, and history carries `tool_use`/`tool_result` content blocks
    rather than JSON strings. Both paths run the same redaction boundary; which one
    is live decides only which branch of `_redacted_turn` does the work.
    """

    model_config = {"arbitrary_types_allowed": True}

    @property
    def _llm_type(self) -> str:
        return "meridian-claude"

    # ---- framework -> ClaudeClient ------------------------------------------------

    def _turn_content(
        self, message: BaseMessage, policy_topic: str | None = None
    ) -> Any:
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
            result = self._tool_result(message.content)
            if self.bound_tools:
                # Native: the provider requires the tool_result block that answers a
                # tool_use to carry that block's own id. `tool_call_id` is the id the
                # provider issued, threaded through `_to_message` — never one we mint,
                # which the provider would reject as an id it never saw.
                tool_use_id = getattr(message, "tool_call_id", None)
                if not isinstance(tool_use_id, str) or not tool_use_id:
                    raise LLMError(
                        "a tool result carries no tool_call_id; native tool calling "
                        "cannot pair it with the tool_use block it answers"
                    )
                return [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_use_id,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                ]
            payload: dict[str, Any] = {}
            name = getattr(message, "name", None)
            if isinstance(name, str) and name:
                payload["tool"] = name
            payload["result"] = result
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
            args = call.get("args")
            if self.bound_tools:
                call_id = call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    raise LLMError(
                        "a tool call carries no id; native tool calling cannot pair it "
                        "with the tool_result block that answers it"
                    )
                # The model-authored query is dropped here exactly as it is on the
                # JSON path. `_redacted_tool_use` would mask it anyway, but a boundary
                # that holds only because the redactor catches it is one allowlist
                # entry from leaking. The provider pairs turns by id, not by input, so
                # sending the block with no input is well-formed.
                block_input = _replayed_input(name, args, policy_topic)
                return [
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": name,
                        "input": block_input,
                    }
                ]
            action: dict[str, Any] = {"action": "tool", "tool": name}
            replayed = _replayed_input(name, args, policy_topic)
            if replayed:
                action["input"] = replayed
            return json.dumps(action)

        if self.bound_tools:
            # A prose-only assistant turn has no block shape this boundary carries
            # (`_redacted_blocks` refuses `text` blocks, because prose cannot be
            # masked). It also cannot be dropped silently: the provider would then see
            # a tool_result with no tool_use before it. The seam never produces one —
            # `_to_message` emits either a tool call or a final answer — so this is a
            # fail-closed guard on a path only a framework change could reach.
            raise LLMError(
                "a history turn carries neither a tool call nor a tool result; native "
                "tool calling has no block shape for prose"
            )
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

    def _split(self, messages: list[BaseMessage]) -> tuple[str, list[dict]]:
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

        # Read off the OFFICER's request, never off the model's echoed tool input --
        # same rule as `_officer_context` below, and for the same reason: the value
        # goes back to the model as the argument its own search is replayed with.
        policy_topic = self._officer_policy_topic(request_json)

        history = []
        for message in messages[first_human + 1 :]:
            role = ROLE_BY_MESSAGE_TYPE.get(message.type)
            if role is None:
                raise LLMError(f"unsupported message type {message.type!r} in history")
            history.append(
                {"role": role, "content": self._turn_content(message, policy_topic)}
            )
        return request_json, history

    @staticmethod
    def _officer_policy_topic(request_json: str) -> str | None:
        """The officer's policy topic code, or None when they asked no policy question.

        Tolerant on purpose: `_officer_context` is the one that refuses a malformed
        request, and it runs on the same string a few lines later in `_generate`. A
        second raise here would only change which message an operator sees. An
        unlisted code is not filtered here either -- `_redacted_tool_use` masks it at
        the boundary, which is where the vocabulary lives.
        """
        try:
            parsed = json.loads(request_json)
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        topic = parsed.get("policy_topic")
        return topic if isinstance(topic, str) and topic else None

    # ---- ClaudeClient -> framework ------------------------------------------------

    @staticmethod
    def _officer_context(request_json: str) -> tuple[int, str]:
        """The officer's real application id and task, never the model's.

        `request_json` is always `json.dumps({"application_id": ..., "task": ...})` —
        the shape `assistant.run` builds and every caller of this seam sends. Parsed
        here (not trusted from the model's echoed tool `input`) so a tool call can be
        pinned to the id the officer actually asked about.
        """
        try:
            parsed = json.loads(request_json)
        except json.JSONDecodeError as exc:
            raise LLMError(
                "the officer's request must be a JSON object with application_id "
                "and task"
            ) from exc
        if not isinstance(parsed, dict):
            raise LLMError(
                "the officer's request must be a JSON object with application_id "
                "and task"
            )
        app_id = parsed.get("application_id")
        task = parsed.get("task")
        if not isinstance(app_id, int) or isinstance(app_id, bool):
            raise LLMError("the officer's request must carry an integer application_id")
        if not isinstance(task, str) or not task:
            raise LLMError("the officer's request must carry a non-empty task")
        return app_id, task

    @staticmethod
    def _already_scored(messages: list[BaseMessage]) -> bool:
        """Whether `score_application` already ran earlier in this message list.

        Mirrors `assistant.run`'s `score_result is None` cache: a `ToolMessage` named
        `score_application` only exists if the framework already dispatched and got a
        result back, so a repeat request in the same run must not trigger a second
        regulated credit pull.
        """
        return any(
            isinstance(m, ToolMessage)
            and getattr(m, "name", None) == "score_application"
            for m in messages
        )

    @staticmethod
    def _to_message(
        action: Any, application_id: int, task: str, already_scored: bool
    ) -> AIMessage:
        """Translate one validated `decision_assistant` action into an `AIMessage`.

        A tool action becomes a framework tool call so the caller dispatches through the
        framework rather than by re-parsing text. A final action keeps its validated JSON
        object as the content: the loop validates that answer against the persisted
        decision record, and it must receive the same object the schema admitted.

        `score_application` and `get_decision_record` both take only the application id,
        and it is always `application_id` — the officer's own request — never the
        model's echoed `input`, the same pinning `assistant.run` does before dispatching
        either tool. A `score_application` request is rewritten to `get_decision_record`
        when `task == "explain"` (read-only: never a fresh credit pull) or when
        `already_scored` (the cached-result branch: never a second regulated event in
        one run). `search_policy` is the one tool whose `input` is model-authored
        (the query) and is forwarded as-is.
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
            if name == QUERY_TOOL:
                raw_input = action.get("input")
                args: dict[str, Any] = raw_input if isinstance(raw_input, dict) else {}
            elif name in ("score_application", "get_decision_record"):
                if name == "score_application" and (
                    task == "explain" or already_scored
                ):
                    name = "get_decision_record"
                args = {"application_id": application_id}
            else:
                raise LLMError(f"decision_assistant requested unknown tool {name!r}")
            # The provider's own tool_use id when this was a native tool turn
            # (`client._tool_action` carries it through), so the tool_result block on
            # the next turn can echo the id the provider issued. The minted fallback
            # serves the JSON-action path, where no provider id exists because the
            # provider was never told a tool existed.
            tool_use_id = action.get("tool_use_id")
            if not isinstance(tool_use_id, str) or not tool_use_id:
                tool_use_id = f"call_{uuid.uuid4().hex}"
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": name,
                        "args": args,
                        "id": tool_use_id,
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
        application_id, task = self._officer_context(request_json)
        already_scored = self._already_scored(messages)
        action = self.client.complete(
            _PROMPT_NAME,
            history=history,
            tools=self.bound_tools or None,
            **{_REQUEST_VAR: request_json},
        )
        message = self._to_message(action, application_id, task, already_scored)
        return ChatResult(generations=[ChatGeneration(message=message)])

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
        """Bind the framework's tools as real provider tool schemas.

        Returns a copy carrying the schemas; `_generate` sends them, so the provider
        decides tool calls under a schema rather than being asked in prose to emit a
        JSON object. `build_request._validated_tools` re-validates the shape at the
        boundary — this method's job is the translation and the agreement check.

        The agreement check is the point of failing here rather than at the provider.
        Three lists have to name the same tools: what the framework hands us, what
        `decision_assistant`'s output schema admits, and (checked in
        `assistant._build_agent`) what `_TOOLS` can dispatch. A tool added to one and
        not the others is a runtime 400 or a silent no-op; here it is an LLMError with
        the difference in it.

        `kwargs` is refused rather than ignored: `tool_choice` is decided in the
        adapter (`disable_parallel_tool_use`, because the protocol carries one action
        per turn), and a caller believing it had set one here would be wrong about the
        request that went out.
        """
        if kwargs:
            raise LLMError(
                f"bind_tools takes no options here; got {sorted(kwargs)!r}. tool_choice "
                "is set in the adapter for every tool-bearing request"
            )
        schemas = [_tool_schema(tool) for tool in tools or []]
        if not schemas:
            raise LLMError("bind_tools was given no tools")
        bound = sorted(schema["name"] for schema in schemas)
        declared = sorted(_declared_tool_names())
        if bound != declared:
            raise LLMError(
                f"the framework's tool set {bound!r} does not match the tools "
                f"decision_assistant declares {declared!r}; the model would be bound "
                f"to a tool its output schema cannot name, or told about one it cannot "
                f"call"
            )
        return self.model_copy(update={"bound_tools": schemas})
