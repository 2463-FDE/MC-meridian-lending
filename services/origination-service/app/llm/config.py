"""Concern 1 — Config.

Model id, default params, and timeout live here in one place. The API key is
loaded from the environment only (never hardcoded, unlike the bureau keys in
`app/config.py`). `load_llm_config()` fails loud at boot if the key is missing,
so a misconfigured deploy dies at startup instead of on the first customer call.

Two providers are supported (`provider`): `"anthropic"` (direct API, needs
`CLAUDE_API_KEY`) and `"bedrock"` (Claude on Amazon Bedrock, needs AWS
credentials — see `BedrockAdapter`, not `CLAUDE_API_KEY`).
"""
import os
from dataclasses import dataclass, field

from .errors import LLMConfigError

# Haiku 4.5 — fastest/cheapest, appropriate for loan summarization (ADR 0005).
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# Bedrock model ids are provider-specific (cross-region inference profile id).
# Confirm the exact id enabled in your account/region before relying on this.
_DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_PROVIDERS = ("anthropic", "bedrock")


@dataclass(frozen=True)
class LLMConfig:
    """Immutable client configuration. Build via `load_llm_config()`.

    The credential (`api_key`) is kept out of the default repr/str
    (`repr=False`) so it cannot leak via `log.info(config)`, an exception that
    dumps locals, or a traceback. The redactor does NOT catch API keys (it
    targets PII patterns), so keeping the secret out of every string
    representation is the guardrail. Log via `redacted()` only.

    `api_key` is empty for `provider="bedrock"` — Bedrock auth is AWS
    credentials, held by `BedrockAdapter`/`boto3`, never by this config.
    """

    api_key: str = field(repr=False)
    provider: str = "anthropic"
    model: str = _DEFAULT_MODEL
    timeout: float = 30.0            # seconds, enforced on every call
    max_retries: int = 3            # attempts for transient (429/5xx) failures
    max_tokens: int = 1024          # response cap sent to the provider
    temperature: float = 0.0        # deterministic summaries
    token_budget: int = 20_000      # per-request ceiling; refuse if exceeded
    aws_region: str | None = None   # bedrock only; None lets boto3 resolve it

    def redacted(self) -> dict:
        """Config safe to log — never includes the credential."""
        return {
            "provider": self.provider,
            "model": self.model,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "token_budget": self.token_budget,
            "aws_region": self.aws_region,
        }

    def __str__(self) -> str:  # never render the secret, even via str()
        return f"LLMConfig({self.redacted()})"


def load_llm_config() -> LLMConfig:
    """Load config from the environment.

    For `CLAUDE_PROVIDER=anthropic` (default): raises `LLMConfigError` if
    `CLAUDE_API_KEY` is missing. For `CLAUDE_PROVIDER=bedrock`: `CLAUDE_API_KEY`
    is not required — AWS credentials are resolved by `boto3`/`BedrockAdapter`
    at call time (env, profile, or IAM role), not validated here, since boto3
    supports auth methods (SSO, instance role) this function can't detect from
    env vars alone.

    Numeric env vars are range-checked (timeout, retries, tokens, temperature,
    budget); an out-of-range value raises LLMConfigError rather than silently
    producing a client that fails or misbehaves on the first call.

    Call this at application startup (fail loud at boot) — not lazily on first use.
    """
    provider = os.getenv("CLAUDE_PROVIDER", "anthropic")
    if provider not in _PROVIDERS:
        raise LLMConfigError(
            f"CLAUDE_PROVIDER={provider!r} is not one of {_PROVIDERS}."
        )

    api_key = os.getenv("CLAUDE_API_KEY", "")
    if provider == "anthropic" and not api_key:
        raise LLMConfigError(
            "CLAUDE_API_KEY is not set. The LLM client cannot start without it "
            "for provider=anthropic. Set it in the environment (never hardcode "
            "it), or set CLAUDE_PROVIDER=bedrock to use AWS credentials instead."
        )

    def _num(env: str, default, cast):
        raw = os.getenv(env)
        if raw is None:
            return default
        try:
            return cast(raw)
        except ValueError:
            raise LLMConfigError(f"{env}={raw!r} is not a valid {cast.__name__}.")

    timeout = _num("CLAUDE_TIMEOUT", 30.0, float)
    max_retries = _num("CLAUDE_MAX_RETRIES", 3, int)
    max_tokens = _num("CLAUDE_MAX_TOKENS", 1024, int)
    temperature = _num("CLAUDE_TEMPERATURE", 0.0, float)
    token_budget = _num("CLAUDE_TOKEN_BUDGET", 20_000, int)

    # A value that casts cleanly can still be nonsensical. Reject out-of-range
    # config at boot (fail loud) instead of letting it corrupt calls later:
    #   timeout<=0      -> libpq/httpx "no timeout" or every call errors
    #   max_retries<0   -> retry loop math underflows / no attempts
    #   max_tokens<=0   -> provider rejects the request
    #   temperature outside [0,1] -> provider 4xx on every call
    #   token_budget<max_tokens -> build_request reserves max_tokens for the answer
    #                              and refuses EVERY request before the network
    # (Per-request prompt+history overhead is still checked at call time in
    # build_request via TokenBudgetExceeded; only the max_tokens floor is knowable
    # here, since prompt size varies per request.)
    if timeout <= 0:
        raise LLMConfigError(f"CLAUDE_TIMEOUT must be > 0, got {timeout}.")
    if max_retries < 0:
        raise LLMConfigError(f"CLAUDE_MAX_RETRIES must be >= 0, got {max_retries}.")
    if max_tokens <= 0:
        raise LLMConfigError(f"CLAUDE_MAX_TOKENS must be > 0, got {max_tokens}.")
    if not 0.0 <= temperature <= 1.0:
        raise LLMConfigError(
            f"CLAUDE_TEMPERATURE must be within [0.0, 1.0], got {temperature}."
        )
    if token_budget < max_tokens:
        raise LLMConfigError(
            f"CLAUDE_TOKEN_BUDGET ({token_budget}) must be >= CLAUDE_MAX_TOKENS "
            f"({max_tokens}): every request reserves max_tokens for the answer, so a "
            "smaller budget refuses all requests."
        )

    default_model = _DEFAULT_BEDROCK_MODEL if provider == "bedrock" else _DEFAULT_MODEL
    return LLMConfig(
        api_key=api_key,
        provider=provider,
        model=os.getenv("CLAUDE_MODEL", default_model),
        timeout=timeout,
        max_retries=max_retries,
        max_tokens=max_tokens,
        temperature=temperature,
        token_budget=token_budget,
        aws_region=os.getenv("AWS_REGION"),
    )
