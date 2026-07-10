"""Loan-application summary prompt (the Week-2 assistant's first real prompt).

Turns a loan application into a short, structured summary for a loan officer.
Output is constrained to a JSON schema so the client can validate it before it
reaches the UI. The prompt is deliberately conservative: summarize only what is
present, never invent facts, and never restate raw PII (the account is already
redacted upstream, but the instruction is defense in depth).
"""
from . import PromptTemplate, register

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "risk_flags", "recommended_next_step"],
    "properties": {
        "summary": {"type": "string"},
        "risk_flags": {
            "type": "array",
            "items": {"type": "string"},
        },
        "recommended_next_step": {
            "type": "string",
            "enum": ["approve_review", "request_docs", "manual_underwrite", "decline_review"],
        },
    },
}

SYSTEM = (
    "You are a loan-origination assistant for a mortgage/consumer lender. "
    "You help a human loan officer triage applications. "
    "Rules:\n"
    "1. Summarize ONLY facts present in the application. Never invent numbers, "
    "names, or history.\n"
    "2. Do NOT restate full account numbers, card numbers, SSNs, emails, or "
    "phone numbers in your output. Refer to them generically (e.g. 'the "
    "applicant').\n"
    "3. A human makes the final decision. Your 'recommended_next_step' is a "
    "triage hint, not a lending decision, and must be one of the allowed values.\n"
    "4. Respond with a single JSON object matching the required schema — no prose "
    "outside the JSON."
)

USER_TEMPLATE = (
    "Summarize this loan application for the reviewing officer.\n\n"
    "Application (JSON):\n{application_json}\n\n"
    "Return JSON with keys: summary (string), risk_flags (array of short "
    "strings), recommended_next_step (one of approve_review, request_docs, "
    "manual_underwrite, decline_review)."
)

EXAMPLES = [
    {
        "user": (
            'Summarize this loan application for the reviewing officer.\n\n'
            'Application (JSON):\n'
            '{"name": "the applicant", "amount": 18000, "term_months": 48, '
            '"annual_income": 42000, "employment_months": 5, "purpose": "auto"}\n'
        ),
        "assistant": (
            '{"summary": "Applicant requests $18,000 over 48 months for an auto '
            'purchase on a $42,000 income; employment tenure is short (5 months).", '
            '"risk_flags": ["short employment tenure", "DTI needs verification"], '
            '"recommended_next_step": "request_docs"}'
        ),
    }
]

register(
    PromptTemplate(
        name="loan_application_summary",
        version="2026-07-07",
        system=SYSTEM,
        user_template=USER_TEMPLATE,
        required_vars=("application_json",),
        json_vars=("application_json",),
        examples=EXAMPLES,
        output_schema=OUTPUT_SCHEMA,
    )
)
