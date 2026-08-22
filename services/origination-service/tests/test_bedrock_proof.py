"""Contracts for the Bedrock exact-SHA proof receipt (scripts/bedrock_proof.py).

Every case here runs WITHOUT credentials and without touching the network — the
script's refusals all return before the call, which is the point: they are what
stops a receipt claiming more than the run supports. CI can therefore assert the
harness has not drifted even though it can never make the call itself.
"""

import json

import pytest

from app.llm.adapter import FakeAdapter
from app.llm.config import LLMConfig
from scripts.bedrock_proof import build_receipt, credential_form, main, run_call

_TOKEN = "ABSK-fake-bearer-value-never-in-a-receipt"


def _config(**over):
    base = dict(
        api_key="",
        provider="bedrock",
        model="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        aws_region="us-east-1",
    )
    base.update(over)
    return LLMConfig(**base)


def _ok_call():
    return {
        "succeeded": True,
        "latency_ms": 812.4,
        "model_returned": "x",
        "stop_reason": "end_turn",
    }


# --- credential form: name it, never carry it ----------------------------


def test_bearer_token_is_the_reported_form():
    assert credential_form({"AWS_BEARER_TOKEN_BEDROCK": _TOKEN}) == (
        "AWS_BEARER_TOKEN_BEDROCK"
    )


def test_key_pair_reported_only_when_both_halves_are_present():
    env = {"AWS_ACCESS_KEY_ID": "AKIA_fake"}
    assert credential_form(env) is None  # secret half missing
    env["AWS_SECRET_ACCESS_KEY"] = "s"
    assert credential_form(env) == "AWS_ACCESS_KEY_ID+AWS_SECRET_ACCESS_KEY"


def test_key_pair_with_session_token_is_reported_as_all_three():
    # Temporary credentials (STS/SSO) add AWS_SESSION_TOKEN alongside the key
    # pair; the SDK signs with it, so the receipt must name it too (RGN-001
    # sibling finding PRF-002) rather than understating the credential used.
    env = {
        "AWS_ACCESS_KEY_ID": "AKIA_fake",
        "AWS_SECRET_ACCESS_KEY": "s",
        "AWS_SESSION_TOKEN": "t",
    }
    assert credential_form(env) == (
        "AWS_ACCESS_KEY_ID+AWS_SECRET_ACCESS_KEY+AWS_SESSION_TOKEN"
    )


def test_key_pair_without_session_token_omits_it():
    env = {"AWS_ACCESS_KEY_ID": "AKIA_fake", "AWS_SECRET_ACCESS_KEY": "s"}
    assert credential_form(env) == "AWS_ACCESS_KEY_ID+AWS_SECRET_ACCESS_KEY"


def test_bearer_wins_when_both_forms_are_set():
    # The SDK prefers the bearer token on the Bedrock path, so naming the key pair
    # would attest a credential the call did not use.
    form = credential_form(
        {
            "AWS_BEARER_TOKEN_BEDROCK": _TOKEN,
            "AWS_ACCESS_KEY_ID": "AKIA_fake",
            "AWS_SECRET_ACCESS_KEY": "s",
        }
    )
    assert form == "AWS_BEARER_TOKEN_BEDROCK"


@pytest.mark.parametrize("env", [{}, {"AWS_BEARER_TOKEN_BEDROCK": "   "}])
def test_no_credential_reports_none(env):
    assert credential_form(env) is None


# --- the receipt only claims what the run supports -----------------------


def test_clean_tree_and_successful_call_is_proof():
    receipt = build_receipt(
        commit="a" * 40,
        tree_clean=True,
        config=_config(),
        credential="AWS_BEARER_TOKEN_BEDROCK",
        call=_ok_call(),
        generated_at="2026-08-22T00:00:00+00:00",
    )
    assert receipt["exact_sha_proof"] is True
    assert receipt["selection"]["aws_region"] == "us-east-1"
    assert receipt["selection"]["provider"] == "bedrock"


def test_dirty_tree_is_never_proof_even_on_a_successful_call():
    # The code that ran is not the code at that commit, so the SHA attests nothing.
    receipt = build_receipt(
        commit="a" * 40,
        tree_clean=False,
        config=_config(),
        credential="AWS_BEARER_TOKEN_BEDROCK",
        call=_ok_call(),
        generated_at="2026-08-22T00:00:00+00:00",
    )
    assert receipt["exact_sha_proof"] is False


def test_failed_call_is_never_proof_on_a_clean_tree():
    receipt = build_receipt(
        commit="a" * 40,
        tree_clean=True,
        config=_config(),
        credential="AWS_BEARER_TOKEN_BEDROCK",
        call={"succeeded": False, "error_class": "LLMHTTPError", "error": "403"},
        generated_at="2026-08-22T00:00:00+00:00",
    )
    assert receipt["exact_sha_proof"] is False


def test_receipt_carries_the_credential_name_not_its_value():
    receipt = build_receipt(
        commit="a" * 40,
        tree_clean=True,
        config=_config(),
        credential="AWS_BEARER_TOKEN_BEDROCK",
        call=_ok_call(),
        generated_at="2026-08-22T00:00:00+00:00",
    )
    assert _TOKEN not in json.dumps(receipt)


# --- refusals: each one prevents a receipt that overclaims ---------------


def _clean_tree(monkeypatch):
    from scripts import bedrock_proof

    monkeypatch.setattr(bedrock_proof, "git_state", lambda: ("b" * 40, True))


def test_refuses_a_dirty_tree_without_the_flag(monkeypatch, capsys):
    from scripts import bedrock_proof

    monkeypatch.setattr(bedrock_proof, "git_state", lambda: ("b" * 40, False))
    assert main([]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_refuses_when_the_provider_is_not_bedrock(monkeypatch, capsys):
    _clean_tree(monkeypatch)
    monkeypatch.setenv("CLAUDE_API_KEY", "k")
    monkeypatch.delenv("CLAUDE_PROVIDER", raising=False)
    assert main([]) == 2
    assert "not 'bedrock'" in capsys.readouterr().err


def test_refuses_when_no_aws_credential_is_present(monkeypatch, capsys):
    _clean_tree(monkeypatch)
    monkeypatch.setenv("CLAUDE_PROVIDER", "bedrock")
    for name in (
        "AWS_BEARER_TOKEN_BEDROCK",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    assert main([]) == 2
    assert "no AWS credential" in capsys.readouterr().err


# --- run_call: must go through ClaudeClient, not a raw adapter call -------
#
# RGN-001 sibling finding PRF-001: a raw `adapter.complete()` call bypasses the
# retry policy, schema validation/guards, and trace stamps ClaudeClient wires
# in. These inject a FakeAdapter (no network, no credentials) to prove run_call
# actually exercises that pipeline instead of calling the adapter directly.


def test_run_call_validates_output_through_the_client_pipeline():
    adapter = FakeAdapter(
        response=json.dumps(
            {
                "summary": "Applicant requests $12,000 over 36 months.",
                "risk_flags": [],
                "recommended_next_step": "approve_review",
            }
        )
    )
    result = run_call(_config(), adapter=adapter)

    assert result["succeeded"] is True
    assert result["validated_result_type"] == "dict"
    # FakeAdapter isn't a BedrockAdapter, so _execution_mode correctly reports
    # "fixture" here — proving the label reflects the injected adapter, not a
    # hardcoded claim of a real call.
    assert result["execution_mode"] == "fixture"
    assert len(adapter.calls) == 1


def test_run_call_reports_a_schema_violation_as_a_failed_call():
    # Model text that is not valid JSON never reaches the schema — validate_structured
    # raises ValidationFailed, which run_call must record as a failure, not let escape
    # as an uncaught exception (a raw adapter.complete() call would never surface this
    # at all, since it skips validation entirely).
    adapter = FakeAdapter(response="not a JSON object")
    result = run_call(_config(), adapter=adapter)

    assert result["succeeded"] is False
    assert result["error_class"] == "ValidationFailed"


def test_a_malformed_region_stops_the_run_before_any_call(monkeypatch):
    # The region guard lives in load_llm_config, which main() calls before it
    # reaches the network — so a near-miss region fails at startup, not mid-demo.
    from app.llm.errors import LLMConfigError

    _clean_tree(monkeypatch)
    monkeypatch.setenv("CLAUDE_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_REGION", "us-east")
    with pytest.raises(LLMConfigError):
        main([])
