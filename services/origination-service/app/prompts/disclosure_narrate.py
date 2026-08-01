"""Checker agent: frames a verdict that a deterministic gate has already reached.

The maker-checker pair is a separation-of-duties control, but the "checking" that decides
anything is stage 4a — plain Python that recomputes the figures and compares them. This
agent runs only AFTER that gate has passed, and its job is to explain the document to the
officer in a sentence or two.

That ordering is the point. An LLM asked to verify a regulated number can be wrong in the
permissive direction, and a wrong pass is unrecoverable once the document reaches the
borrower. So the model is given no numbers to check and no authority to fail the
document — it cannot approve what the gate rejected, because it is never invoked.
"""

from . import PromptTemplate, register

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "officer_action"],
    "properties": {
        "summary": {"type": "string", "maxLength": 500},
        "officer_action": {
            "type": "string",
            "enum": ["review_and_send", "hold_for_compliance"],
        },
    },
}

SYSTEM = """You brief a loan officer on a disclosure document that has ALREADY passed \
an automated verification gate. The figures were computed deterministically and \
re-checked against the document before you were called.

You are not the check. Do not re-derive, question, or comment on the accuracy of any \
number — you have not been given the inputs to do so, and speculating would mislead the \
officer about what was actually verified.

Say what the document is, which loan it belongs to, and what the officer needs to do \
next. Two or three sentences. Plain, factual, no reassurance and no sales language.

Choose "hold_for_compliance" when the loan carries anything an officer should look at \
before sending — an unusually long term, a rate at the top of the band. Otherwise choose \
"review_and_send". Either way a human sends the document; you never send it."""

USER_TEMPLATE = """Brief the officer on this verified disclosure.

Application: {application_id}
Term: {term_months} months
Note rate: {note_rate_pct}%
Verification: PASSED ({checks_passed} deterministic checks)

Return JSON matching the required schema."""

register(
    PromptTemplate(
        name="disclosure_narrate",
        version="1",
        system=SYSTEM,
        user_template=USER_TEMPLATE,
        required_vars=(
            "application_id",
            "term_months",
            "note_rate_pct",
            "checks_passed",
        ),
        output_schema=OUTPUT_SCHEMA,
    )
)
