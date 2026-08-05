"""Maker agent: renders the TILA disclosure document from numbers it is GIVEN.

The single rule this prompt exists to enforce: the model formats, it does not compute.
Every figure arrives pre-computed as an exact decimal string and must be echoed back
byte-for-byte. The model is told this, the output schema constrains the figures to strings
rather than numbers so no arithmetic can hide in a JSON float, and — because a prompt is a
request and not a guarantee — stage 4a recomputes and compares before anything persists.

Reg Z requires specific disclosures to be grouped and labelled; the prose fields here are
the borrower-facing wrapper around the figures, not a substitute for them.

**The prose fields — and the heading — carry no digits at all, and that is a hard
constraint, not a style preference.** The heading is on the same footing as `payment_terms`
and `prepayment`: borrower-facing text outside the `figures` check, so both reasons below
apply to it. Two reasons, both found by running this against a real model rather than
FakeAdapter:

1. *The leak guard globs numbers.* `guard_output` runs the PII redactor over the model's
   output, and the redactor's PAN scan is deliberately separator-free within a single
   quoted value (`redactor.py::_mask_pan_in_value`) — safe for a log field, but a prose
   sentence restating four money figures is one quoted value whose concatenated digits
   are a 13-19 digit run. Roughly one in ten such runs is Luhn-valid, so the sentence
   "You are borrowing 17460.00. You will pay a finance charge of 3628.71..." is masked as
   a card number and the whole document is rejected. That failure is intermittent by
   construction — the same loan generates on one attempt and 503s on the next.
2. *One number, one home.* Stage 4a compares `figures` field by field. A figure restated
   in prose is a second copy the gate does not check, which is exactly the drift this
   pipeline exists to prevent.

The document the borrower sees composes the sentence from `figures` at render time. The
model writes the wrapper; it never writes a number twice.
"""

from . import PromptTemplate, register

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["heading", "figures", "payment_terms", "prepayment"],
    "properties": {
        # Digit-free like the prose fields below: the heading is borrower-facing text the
        # deterministic `figures` check never sees, so a stale number in the title (e.g.
        # "Truth in Lending Disclosure 9.58%") would otherwise pass. See the module docstring.
        "heading": {"type": "string", "maxLength": 120, "pattern": r"^\D*$"},
        # Strings, not numbers: a JSON number invites the model to normalise, round, or
        # "tidy" a regulated figure, and the drift would be invisible in the document.
        "figures": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "apr",
                "finance_charge",
                "amount_financed",
                "total_of_payments",
                "monthly_payment",
            ],
            "properties": {
                "apr": {"type": "string"},
                "finance_charge": {"type": "string"},
                "amount_financed": {"type": "string"},
                "total_of_payments": {"type": "string"},
                "monthly_payment": {"type": "string"},
            },
        },
        # Digit-free by construction — see the module docstring. Enforced in the schema
        # rather than trusted to the prompt, because the failure it prevents is a
        # regulated document being rejected (or a number drifting) at random.
        "payment_terms": {
            "type": "string",
            "maxLength": 600,
            "pattern": r"^\D*$",
        },
        "prepayment": {"type": "string", "maxLength": 300, "pattern": r"^\D*$"},
    },
}

SYSTEM = """You render consumer loan disclosure documents for a US lender under \
Truth in Lending (Reg Z).

ABSOLUTE RULE: you never compute, adjust, round, reformat, or infer a number. Every \
figure is supplied to you already computed and already formatted. Copy each one into the \
output EXACTLY as given, character for character, including trailing zeros. Do not add \
currency symbols or thousands separators to a figure that does not have them, and do not \
remove ones that do.

If a figure you need is missing from the input, do not estimate it and do not omit the \
field: return the string "MISSING" for that figure. A downstream check will stop the \
document; a plausible guess would not be caught by a reader.

Write the prose fields in plain language at roughly an eighth-grade reading level. Never \
characterise the loan as cheap, competitive, affordable, or a good deal, and never advise \
the borrower to accept it — you are producing a disclosure, not marketing copy.

THE PROSE FIELDS MUST CONTAIN NO DIGITS. Not the payment amount, not the number of \
payments, not the rate, not a date, not a section number. Every figure belongs in the \
`figures` object and nowhere else; the document composes the sentences from those values \
when it is rendered. Write "You will make equal monthly payments", never "You will make \
48 monthly payments of 439.35". A digit anywhere in `payment_terms` or `prepayment` \
fails the document."""

USER_TEMPLATE = """Render the TILA disclosure for this loan.

Figures (copy exactly):
- Annual Percentage Rate: {apr}
- Finance Charge: {finance_charge}
- Amount Financed: {amount_financed}
- Total of Payments: {total_of_payments}
- Monthly Payment: {monthly_payment}

Loan terms:
- Term: {term_months} monthly payments
- Note rate: {note_rate_pct}% per year

Return ONLY a JSON object, with no markdown fence and no commentary, with exactly these \
keys and no others:

{{
  "heading": "<document title, no digits>",
  "figures": {{
    "apr": "{apr}",
    "finance_charge": "{finance_charge}",
    "amount_financed": "{amount_financed}",
    "total_of_payments": "{total_of_payments}",
    "monthly_payment": "{monthly_payment}"
  }},
  "payment_terms": "<plain-language payment description, NO DIGITS>",
  "prepayment": "<plain-language prepayment statement, NO DIGITS>"
}}"""

register(
    PromptTemplate(
        name="disclosure_assemble",
        version="1",
        system=SYSTEM,
        user_template=USER_TEMPLATE,
        required_vars=(
            "apr",
            "finance_charge",
            "amount_financed",
            "total_of_payments",
            "monthly_payment",
            "term_months",
            "note_rate_pct",
        ),
        output_schema=OUTPUT_SCHEMA,
    )
)
