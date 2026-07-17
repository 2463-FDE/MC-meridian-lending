"""Simulate one loan-summary LLM call and send its trace to LangSmith.

Run from the service directory (imports are ``app.*``):

    cd services/origination-service
    export LANGSMITH_TRACING=true
    export LANGSMITH_API_KEY=lsv2_...          # host env only, never committed
    export LANGSMITH_PROJECT=2463-FDE

    PYTHONPATH=. python scripts/langsmith_demo.py          # FakeAdapter — no network to Claude, no tokens
    PYTHONPATH=. python scripts/langsmith_demo.py --real   # real Claude call (needs CLAUDE_API_KEY)

Then open https://smith.langchain.com → project 2463-FDE. Expect a trace tree:

    llm.complete            (root: prompt_name only — raw variables are stripped,
      └─ llm.transport       redaction hasn't run yet at that layer)
                            (child: full post-redaction request + completion,
                             token counts, latency)

The synthetic application below carries FAKE PII on purpose — the point of the
demo is seeing it arrive in LangSmith as ``••••`` masks, proving the trace
exports only what the provider itself is allowed to see.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.llm import FakeAdapter  # noqa: E402
from app.llm.client import ClaudeClient  # noqa: E402
from app.llm.config import LLMConfig, load_llm_config  # noqa: E402

# Synthetic applicant. All identifiers are fake (SSN is the canonical test SSN).
APPLICATION = {
    "name": "Maria Santos",
    "ssn": "123-45-6789",
    "dob": "1988-03-14",
    "email": "maria.santos@example.com",
    "phone": "555-867-5309",
    "amount": 12000,
    "term_months": 36,
    "annual_income": 54000,
    "employment_months": 26,
    "purpose": "debt_consolidation",
}

FAKE_MODEL_RESPONSE = json.dumps(
    {
        "summary": (
            "Applicant requests $12,000 over 36 months for debt consolidation "
            "on a $54,000 income; employment tenure is stable (26 months)."
        ),
        "risk_flags": ["DTI needs verification"],
        "recommended_next_step": "request_docs",
    }
)


def main() -> None:
    real = "--real" in sys.argv

    if real:
        client = ClaudeClient(load_llm_config())
        mode = "real Claude API"
    else:
        config = LLMConfig(api_key="fake-key-unused")
        client = ClaudeClient(config, adapter=FakeAdapter(response=FAKE_MODEL_RESPONSE))
        mode = "FakeAdapter (no tokens spent)"

    if os.getenv("LANGSMITH_TRACING", "").lower() != "true":
        print(
            "note: LANGSMITH_TRACING is not 'true' — call will run but no trace is sent"
        )

    print(f"mode: {mode}")
    result = client.summarize_application(json.dumps(APPLICATION))
    print("validated result:")
    print(json.dumps(result, indent=2))

    if os.getenv("LANGSMITH_TRACING", "").lower() == "true":
        # Traces are batched in the background; flush before the process exits.
        from langsmith import Client

        Client().flush()
        project = os.getenv("LANGSMITH_PROJECT", "default")
        print(f"trace sent — check https://smith.langchain.com project {project!r}")


if __name__ == "__main__":
    main()
