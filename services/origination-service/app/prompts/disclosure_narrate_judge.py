"""D3 offline judge prompt: grades a `disclosure_narrate` completion for groundedness.

docs/specs/disclosure-narration-judge.md, Minimum Build Slice #3. Checks the same thing
D1's runtime guard (`_narration_is_grounded`, `disclosure_coordinator.py`) checks
deterministically -- a figure the model was not given is invented -- but reads for meaning
rather than pattern-matching, so it catches a paraphrase or spelled-out figure the regex is
known to miss (spec's Risks section: "Regex false negatives (D1 alone)").

The judge is given the SAME four values `disclosure_narrate` was given, not a subset. The
narrate prompt hands the model an application id and a passed-check count as well as the
term and rate, and its system message asks the model to say which loan the summary covers
(`disclosure_narrate.py` USER_TEMPLATE and SYSTEM). A judge told only about the term and
rate grades those two supplied values as fabrication, which fails correct completions on
axis (a) and then disagrees with D1 -- whose comparison is unit-aware and lets an
unitless application id or check count through -- on axis (c). Withholding context from
the judge that the narrator had makes both axes read a defect that is not there.

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

The model that wrote this narration was given exactly four values: an application id, a \
loan term in months, a note rate as a percent, and the number of deterministic \
verification checks that passed. It was given NO dollar amount at all, and no percent or \
rate other than that one. It was asked to say which application the disclosure belongs \
to, so restating the application id is expected and grounded.

Mark it grounded ONLY if every figure the narration states is one of those four values, \
in the same units it was given -- however it is expressed: digits, words ("twelve \
percent"), or a paraphrase ("a bit over a year"). Any dollar amount is NOT grounded, \
whatever its value, because no money figure was given. A percent other than the note \
rate, a term other than the term given, a check count other than the count given, or any \
other invented figure is NOT grounded.

Return ONE JSON object and nothing else, with exactly one key:

{"grounded": true} or {"grounded": false}"""

USER_TEMPLATE = """The model was given:
Application: {application_id}
Term: {term_months} months
Note rate: {note_rate_pct}%
Verification: PASSED ({checks_passed} deterministic checks)

Grade this narration:
"{summary}"

Return ONLY a JSON object with exactly one key, "grounded", and no others."""

register(
    PromptTemplate(
        name="disclosure_narrate_judge",
        version="1",
        system=SYSTEM,
        user_template=USER_TEMPLATE,
        required_vars=(
            "summary",
            "application_id",
            "term_months",
            "note_rate_pct",
            "checks_passed",
        ),
        output_schema=OUTPUT_SCHEMA,
    )
)
