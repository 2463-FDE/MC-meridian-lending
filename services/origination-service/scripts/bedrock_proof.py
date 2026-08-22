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

The call itself goes through ``ClaudeClient.summarize_application()`` — the
production entry point ``app/main.py`` wires at boot — not a raw adapter call,
so the receipt also proves the retry policy, schema validation/guards, and the
trace-metadata stamps a raw ``adapter.complete()`` call would bypass.
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

from app.llm.adapter import BedrockAdapter, Completion, CompletionRequest  # noqa: E402
from app.llm.client import ClaudeClient, _execution_mode  # noqa: E402
from app.llm.config import load_llm_config  # noqa: E402
from app.llm.errors import LLMError  # noqa: E402

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
_SESSION_TOKEN_ENV = "AWS_SESSION_TOKEN"


def credential_form(env: dict) -> str | None:
    """Name the credential form the environment offers, never its value.

    Bearer wins when both are present: for Bedrock the SDK prefers
    ``AWS_BEARER_TOKEN_BEDROCK``, so reporting the key pair would name a
    credential the call did not use. Returns None when neither is set — the
    caller refuses rather than letting the SDK chain surface it later.

    Temporary credentials (STS/SSO — the common case) add ``AWS_SESSION_TOKEN``
    alongside the key pair; the SDK includes it in the signed request, so a
    receipt naming only the key pair would understate the credential actually
    used (review finding PRF-002).
    """
    if env.get(_BEARER_ENV, "").strip():
        return _BEARER_ENV
    if all(env.get(name, "").strip() for name in _KEY_PAIR_ENV):
        if env.get(_SESSION_TOKEN_ENV, "").strip():
            return "AWS_ACCESS_KEY_ID+AWS_SECRET_ACCESS_KEY+AWS_SESSION_TOKEN"
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


class _RecordingBedrockAdapter(BedrockAdapter):
    """BedrockAdapter that remembers the last real Completion it returned.

    `ClaudeClient` does not hand the raw `Completion` back to its caller (it
    returns the validated result, per `complete()`'s contract), and the receipt
    needs provider-echoed fields (model, stop_reason, token counts) that only
    exist on that `Completion`. Subclassing rather than wrapping keeps
    `isinstance(adapter, BedrockAdapter)` true, so `_execution_mode` still
    reports "real" for this call exactly as it would in production.
    """

    def __init__(self, region: str | None = None):
        super().__init__(region=region)
        self.last_completion: Completion | None = None

    def complete(self, req: CompletionRequest) -> Completion:
        completion = super().complete(req)
        self.last_completion = completion
        return completion


def run_call(config, *, adapter: BedrockAdapter | None = None) -> dict:
    """One real call through the production path. Returns the call half of the receipt.

    Goes through `ClaudeClient.summarize_application()` — the same entry point
    `app/main.py` wires at boot — not a raw adapter call, so the receipt also
    proves the retry policy (transport), schema validation/guards, and the
    `llm_provider`/`execution_mode` trace stamps that a raw `adapter.complete()`
    call would bypass. `adapter` is injectable for tests; production always
    supplies a fresh `_RecordingBedrockAdapter`.

    No extra retry here beyond what `ClaudeClient` already does: a proof run
    reports what happened on the call it made. The response text is recorded
    as a length and a SHA-256 rather than verbatim — the model is answering
    about a synthetic applicant, but a receipt is an artifact people paste
    around, and a hash proves a response arrived without making the artifact a
    place model output accumulates.
    """
    adapter = (
        adapter
        if adapter is not None
        else _RecordingBedrockAdapter(region=config.aws_region)
    )
    client = ClaudeClient(config, adapter=adapter)

    t0 = perf_counter()
    try:
        result = client.summarize_application(json.dumps(APPLICATION))
    except LLMError as exc:
        return {
            "succeeded": False,
            "latency_ms": round((perf_counter() - t0) * 1000, 1),
            "error_class": type(exc).__name__,
            "error": str(exc),
        }
    completion = getattr(adapter, "last_completion", None)
    return {
        "succeeded": True,
        "latency_ms": round((perf_counter() - t0) * 1000, 1),
        "execution_mode": _execution_mode(adapter),
        "validated_result_type": type(result).__name__,
        # The model id the PROVIDER echoed back. The strongest single element in
        # here: it is the one field the local configuration did not choose.
        "model_returned": completion.model if completion else None,
        "stop_reason": completion.stop_reason if completion else None,
        "input_tokens": completion.input_tokens if completion else None,
        "output_tokens": completion.output_tokens if completion else None,
        "response_chars": len(completion.text) if completion else None,
        "response_sha256": (
            hashlib.sha256(completion.text.encode()).hexdigest() if completion else None
        ),
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
