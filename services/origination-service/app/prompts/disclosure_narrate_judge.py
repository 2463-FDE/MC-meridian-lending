"""D3 offline judge prompt: grades a `disclosure_narrate` completion for groundedness.

docs/specs/disclosure-narration-judge.md, Minimum Build Slice #3. Checks the same thing
D1's runtime guard (`_narration_is_grounded`, `disclosure_coordinator.py`) checks
deterministically -- the model was given only a term in months and a note rate, so any
other dollar amount, percent figure, or count in its `summary` is invented -- but reads
for meaning rather than pattern-matching, so it catches a paraphrase or spelled-out
figure the regex is known to miss (spec's Risks section: "Regex false negatives (D1
alone)").

Reuses `ClaudeClient` (app/llm/client.py); no new provider dependency (spec D3, "Why not
reuse rag_eval").
"""

from . import PromptTemplate, register

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["grounded"],
    "properties": {
        "grounded": {"type": "boolean"},
    },
}

SYSTEM = """You grade one officer-facing loan disclosure narration for groundedness. \
You do not rewrite it and you do not decide anything about the loan.

The model that wrote this narration was given exactly two numbers: a loan term in \
months and a note rate as a percent. It was given NO dollar amount at all, and no \
percent or rate other than that one.

Mark it grounded ONLY if the narration states no figure beyond those two -- no dollar \
amount, no percent other than the note rate given, and no other count or figure, \
however it is expressed: digits, words ("twelve percent"), or a paraphrase ("a bit \
over a year"). A figure that restates the same term or rate it was given, in the same \
units, is grounded. Anything else is NOT grounded.

Return ONE JSON object and nothing else, with exactly one key:

{"grounded": true} or {"grounded": false}"""

USER_TEMPLATE = """The model was given:
Term: {term_months} months
Note rate: {note_rate_pct}%

Grade this narration:
"{summary}"

Return ONLY a JSON object with exactly one key, "grounded", and no others."""

register(
    PromptTemplate(
        name="disclosure_narrate_judge",
        version="1",
        system=SYSTEM,
        user_template=USER_TEMPLATE,
        required_vars=("summary", "term_months", "note_rate_pct"),
        output_schema=OUTPUT_SCHEMA,
    )
)
