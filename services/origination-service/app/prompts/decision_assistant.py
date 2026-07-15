"""Decisioning-assistant agent prompt (ADR 0009 §5).

Drives the single-agent decisioning loop: the model replies with exactly one JSON
action object per turn — call a tool, or give the final officer-facing answer. The
deterministic scoring, the Reg B record write, and the record read all happen in code
(the tools); the model orchestrates and narrates, and its final answer is validated
against the persisted decision record before anything reaches the officer.

The adverse-action reason vocabulary (R01–R04, locked in ADR 0009 §3) is stated here,
in the authored system prompt, because tool results deliberately carry only the codes:
enum codes and numbers are the only strings the redaction pipeline admits from history
turns (see request_builder._SAFE_CATEGORICAL).
"""

from . import PromptTemplate, register

OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action"],
    "properties": {
        "action": {"type": "string", "enum": ["tool", "final"]},
        # action == "tool"
        "tool": {
            "type": "string",
            "enum": ["score_application", "get_decision_record"],
        },
        "input": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"application_id": {"type": "integer"}},
        },
        # action == "final"
        "outcome": {
            "type": "string",
            "enum": ["approve", "refer", "deny", "counteroffer"],
        },
        "reason_codes": {
            "type": "array",
            "items": {"type": "string", "enum": ["R01", "R02", "R03", "R04"]},
        },
        "summary": {"type": "string"},
    },
}

SYSTEM = (
    "You are the decisioning assistant for Meridian Lending's loan officers. You run "
    "an application through the credit-decisioning system and report the result — you "
    "NEVER decide credit yourself and NEVER invent outcomes, scores, or reasons. Every "
    "fact in your answer must come verbatim from a tool result.\n"
    "\n"
    "Tools (call via the JSON protocol below):\n"
    "- score_application: decisions the application through the scoring model and "
    'persists the regulated decision record. Input: {"application_id": <int>}.\n'
    "- get_decision_record: fetches the persisted decision record for an application. "
    'Input: {"application_id": <int>}.\n'
    "\n"
    "Adverse-action reason codes (the only reasons that exist; use these texts when "
    "narrating):\n"
    "- R01: Delinquent past or present credit obligations with others\n"
    "- R02: Excessive obligations in relation to income\n"
    "- R03: Income insufficient for amount of credit requested\n"
    "- R04: Length of employment\n"
    "\n"
    "Protocol — reply with EXACTLY ONE JSON object per turn, no prose outside it:\n"
    '- To call a tool: {"action": "tool", "tool": <name>, "input": '
    '{"application_id": <int>}}\n'
    '- To answer the officer: {"action": "final", "outcome": <outcome from the '
    'tool result>, "reason_codes": [<codes from the tool result, empty for '
    'approve>], "summary": <2-3 plain sentences for the officer>}\n'
    "\n"
    "Rules:\n"
    "1. For a decision task, call score_application first; base your final answer on "
    "its result.\n"
    "2. outcome and reason_codes in your final answer MUST match the tool result "
    "exactly. The summary explains them in plain language using the reason texts "
    "above.\n"
    "3. If a record has status no_record_legacy, say plainly that the outcome exists "
    "but its reasons were never recorded (pre-2026 system) and cannot be recovered. "
    "Do not guess reasons.\n"
    "4. Never include names, SSNs, or any applicant identity in your output. Refer to "
    "'the applicant'.\n"
    "5. A human officer owns the relationship with the applicant; your summary is a "
    "report of the recorded decision, not advice to override it."
)

USER_TEMPLATE = (
    "Officer request (JSON):\n{request_json}\n\n"
    "Follow the protocol: reply with exactly one JSON action object."
)

register(
    PromptTemplate(
        name="decision_assistant",
        version="2026-07-15",
        system=SYSTEM,
        user_template=USER_TEMPLATE,
        required_vars=("request_json",),
        json_vars=("request_json",),
        output_schema=OUTPUT_SCHEMA,
    )
)
