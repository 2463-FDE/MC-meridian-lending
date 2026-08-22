"""Capture a reproducible, commit-pinned receipt for one real Claude-on-Bedrock call.

The 2026-09-02 freeze asks for proof of the exact-SHA Bedrock path. A call that
worked once in a shell is not that: the claim is "this commit, this provider, this
region, this model, this response". This script makes the call and writes the
receipt.

Run from the service directory (imports are ``app.*``):

    cd services/origination-service
    export CLAUDE_PROVIDER=bedrock
    export AWS_BEARER_TOKEN_BEDROCK=...        # host env only, never committed
    PYTHONPATH=. python scripts/bedrock_proof.py

    PYTHONPATH=. python scripts/bedrock_proof.py --out /tmp/proof.json
    PYTHONPATH=. python scripts/bedrock_proof.py --allow-dirty   # receipt, but NOT proof

Default output is ``logs/bedrock-proof-<sha>.json`` (``logs/`` is gitignored — a
receipt is a run artifact, not source). The JSON also goes to stdout.

Three things it refuses to do, because each would produce a receipt that claims
more than the run supports:

- **Run against a dirty tree** without ``--allow-dirty``. An exact-SHA claim over
  uncommitted edits is false: the code that ran is not the code at that SHA. With
  the flag it still runs, and the receipt carries ``exact_sha_proof: false``.
- **Run off the bedrock path.** ``CLAUDE_PROVIDER`` must resolve to ``bedrock``;
  otherwise the receipt would attest a direct-Anthropic call.
- **Run with no AWS credential in the environment.** ``AnthropicBedrock`` resolves
  credentials through the SDK chain, so without this check the script would reach
  a confusing SDK error instead of naming the missing input.

The credential VALUE never enters the receipt — only the name of the environment
variable that supplied it. The applicant below is synthetic (the canonical test
SSN), and it goes through the real prompt path, so the request the provider sees
is the redacted one.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from time import perf_counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.llm.adapter import BedrockAdapter  # noqa: E402
from app.llm.config import load_llm_config  # noqa: E402
from app.llm.errors import LLMError  # noqa: E402
from app.llm.request_builder import build_request  # noqa: E402
from app.prompts import get_prompt  # noqa: E402

# Synthetic applicant, same shape and same fake identifiers as scripts/langsmith_demo.py.
# Redaction happens inside build_request for the prompt's declared json_vars, so the
# provider receives masks — a proof run must not be the one place that ships real PII.
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

_BEARER_ENV = "AWS_BEARER_TOKEN_BEDROCK"
_KEY_PAIR_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")


def credential_form(env: dict) -> str | None:
    """Name the credential form the environment offers, never its value.

    Bearer wins when both are present: for Bedrock the SDK prefers
    ``AWS_BEARER_TOKEN_BEDROCK``, so reporting the key pair would name a
    credential the call did not use. Returns None when neither is set — the
    caller refuses rather than letting the SDK chain surface it later.
    """
    if env.get(_BEARER_ENV, "").strip():
        return _BEARER_ENV
    if all(env.get(name, "").strip() for name in _KEY_PAIR_ENV):
        return "AWS_ACCESS_KEY_ID+AWS_SECRET_ACCESS_KEY"
    return None


def git_state() -> tuple[str, bool]:
    """(commit sha, tree is clean). Both come from git, not from an argument."""
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], capture_output=True, text=True, check=True
    ).stdout.strip()
    return sha, not dirty


def build_receipt(
    *,
    commit: str,
    tree_clean: bool,
    config,
    credential: str,
    call: dict,
    generated_at: str,
) -> dict:
    """Assemble the receipt. Pure — no git, no network, no environment reads.

    ``exact_sha_proof`` is the whole point of the artifact and is true only when
    the call succeeded AND the tree was clean: either half missing leaves a
    receipt of something, but not proof of this commit's Bedrock path.
    """
    return {
        "artifact": "bedrock-exact-sha-proof",
        "generated_at": generated_at,
        "commit": commit,
        "tree_clean": tree_clean,
        "exact_sha_proof": bool(tree_clean and call.get("succeeded")),
        "selection": {
            # What the CODE resolved, not what the environment was asked for.
            "provider": config.provider,
            "model": config.model,
            "aws_region": config.aws_region,
            "credential_form": credential,
        },
        "call": call,
    }


def run_call(config) -> dict:
    """One real Bedrock call. Returns the call half of the receipt.

    No retry: a proof run reports what happened on the attempt it made. The
    response text is recorded as a length and a SHA-256 rather than verbatim —
    the model is answering about a synthetic applicant, but a receipt is an
    artifact people paste around, and a hash proves a response arrived without
    making the artifact a place model output accumulates.
    """
    template = get_prompt("loan_application_summary")
    built = build_request(
        template,
        model=config.model,
        max_tokens=config.max_tokens,
        temperature=config.temperature,
        timeout=config.timeout,
        token_budget=config.token_budget,
        application_json=json.dumps(APPLICATION),
    )
    adapter = BedrockAdapter(region=config.aws_region)

    t0 = perf_counter()
    try:
        completion = adapter.complete(built.request)
    except LLMError as exc:
        return {
            "succeeded": False,
            "latency_ms": round((perf_counter() - t0) * 1000, 1),
            "error_class": type(exc).__name__,
            "error": str(exc),
        }
    return {
        "succeeded": True,
        "latency_ms": round((perf_counter() - t0) * 1000, 1),
        # The model id the PROVIDER echoed back. The strongest single element in
        # here: it is the one field the local configuration did not choose.
        "model_returned": completion.model,
        "stop_reason": completion.stop_reason,
        "input_tokens": completion.input_tokens,
        "output_tokens": completion.output_tokens,
        "response_chars": len(completion.text),
        "response_sha256": hashlib.sha256(completion.text.encode()).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out",
        default=None,
        help="receipt path (default logs/bedrock-proof-<sha>.json)",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="run against a dirty tree; the receipt records exact_sha_proof: false",
    )
    args = parser.parse_args(argv)

    commit, tree_clean = git_state()
    if not tree_clean and not args.allow_dirty:
        print(
            "REFUSED: the tree is dirty, so this run cannot prove the path at "
            f"{commit[:12]} — the code running is not the code at that commit. "
            "Commit first, or pass --allow-dirty for a receipt that says so.",
            file=sys.stderr,
        )
        return 2

    config = load_llm_config()
    if config.provider != "bedrock":
        print(
            f"REFUSED: provider resolved to {config.provider!r}, not 'bedrock'. "
            "Set CLAUDE_PROVIDER=bedrock — a receipt from the direct-Anthropic "
            "path would attest the wrong route.",
            file=sys.stderr,
        )
        return 2

    credential = credential_form(os.environ)
    if credential is None:
        print(
            f"REFUSED: no AWS credential in the environment. Set {_BEARER_ENV} "
            f"(preferred) or {'+'.join(_KEY_PAIR_ENV)}. AnthropicBedrock resolves "
            "credentials through the SDK chain, so without this check the run "
            "fails later with a less specific error.",
            file=sys.stderr,
        )
        return 2

    receipt = build_receipt(
        commit=commit,
        tree_clean=tree_clean,
        config=config,
        credential=credential,
        call=run_call(config),
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )

    out = args.out or os.path.join("logs", f"bedrock-proof-{commit[:12]}.json")
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "w") as fh:
        json.dump(receipt, fh, indent=2, sort_keys=True)
        fh.write("\n")

    print(json.dumps(receipt, indent=2, sort_keys=True))
    print(f"\nreceipt written to {out}", file=sys.stderr)
    return 0 if receipt["call"]["succeeded"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
