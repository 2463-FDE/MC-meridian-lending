"""The prompt text must name the keys its output schema requires.

`request_builder` never sends `output_schema` to the model, so the schema is a post-hoc
validator of the FINAL answer, not a constraint the provider enforces. (Tool calls are a
different matter since the loop swap: those are bound as real provider tool schemas with
`tool_choice`, and `client.complete` refuses a tool call that arrives as text. The output
schema still governs the final answer, which travels as text.) The only thing that tells
the model which key names to emit is the prompt text itself. When the two disagree the model invents plausible key names
and `validate_structured` rejects a completion that cost tokens and, for a pipeline stage
with no fallback, fails the request.

That is not hypothetical: `disclosure_narrate` shipped with a schema requiring
`summary`/`officer_action` and a template that said only "Return JSON matching the required
schema". Against a real model (haiku-4-5, 2026-08-01) it answered with `action`,
`document_type`, `key_terms`, `next_step` — every key invented — and the disclosure pipeline
503'd on an application whose figures had already passed the deterministic gate. The
coordinator tests could not catch it: they inject the response text, so the prompt template
is never rendered against a model.

Schema-enforced tool use is now wired into `build_request`, so this invariant no longer
covers tool calls. It stays load-bearing for the final answer, which is still text the
model has to key correctly.
"""

from app.prompts import get_prompt, list_prompts


def test_every_structured_prompt_names_its_required_keys():
    offenders = {}
    for name in list_prompts():
        template = get_prompt(name)
        schema = template.output_schema
        if not schema:
            continue
        text = f"{template.system}\n{template.user_template}"
        missing = [key for key in schema.get("required", ()) if key not in text]
        if missing:
            offenders[name] = missing

    assert not offenders, (
        "these prompts require output keys their own text never mentions, so the model "
        f"has to guess them: {offenders}"
    )
