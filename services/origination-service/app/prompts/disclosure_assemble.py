"""Maker agent: renders the TILA disclosure document from numbers it is GIVEN.

The single rule this prompt exists to enforce: the model formats, it does not compute.
Every figure arrives pre-computed as an exact decimal string and must be echoed back
byte-for-byte. The model is told this, the output schema constrains the figures to strings
rather than numbers so no arithmetic can hide in a JSON float, and — because a prompt is a
request and not a guarantee — stage 4a recomputes and compares before anything persists.

Reg Z requires specific disclosures to be grouped and labelled; the prose fields here are
the borrower-facing wrapper around the figures, not a substitute for them.
"""

from . import PromptTemplate, register

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["heading", "figures", "payment_terms", "prepayment"],
    "properties": {
        "heading": {"type": "string", "maxLength": 120},
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
        "payment_terms": {"type": "string", "maxLength": 600},
        "prepayment": {"type": "string", "maxLength": 300},
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

Write the prose fields in plain language at roughly an eighth-grade reading level. State \
what the borrower will pay and when. Never characterise the loan as cheap, competitive, \
affordable, or a good deal, and never advise the borrower to accept it — you are \
producing a disclosure, not marketing copy."""

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

Return the disclosure as JSON matching the required schema."""

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
