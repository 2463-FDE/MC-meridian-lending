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
from .logging_setup import get_llm_logger

# Haiku 4.5 — fastest/cheapest, appropriate for loan summarization (ADR 0005).
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# Bedrock model ids are provider-specific (cross-region inference profile id).
# Confirm the exact id enabled in your account/region before relying on this.
_DEFAULT_BEDROCK_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"

_PROVIDERS = ("anthropic", "bedrock")

# Pinned Bedrock region. An unset AWS_REGION let boto3 resolve the region from the
# host's ambient config, so the same commit could reach different regions on two
# runs — the freeze requires a reproducible selection, so the literal lives here
# and AWS_REGION only overrides it. `.env.example` documents the same value.
_DEFAULT_BEDROCK_REGION = "us-east-1"
# Regions where the bedrock-runtime endpoint is available (source of truth:
# https://docs.aws.amazon.com/bedrock/latest/userguide/endpoints-region-availability.html,
# checked 2026-08-22). An override must name a region Bedrock actually serves,
# not merely something region-shaped — a shape-only regex (two-letter prefix +
# hyphenated words + digit suffix) accepted a typo like "us-eats-1" or a
# fabricated "zz-fake-1" (review finding RGN-001) and let it reach the SDK
# instead of failing at boot. This list is NOT the general AWS region list:
# ap-east-1 (Hong Kong) is a real AWS region but does not serve bedrock-runtime,
# so it is deliberately absent (review finding RGN-002 — an earlier version of
# this allowlist used the general AWS region set, which was too loose in one
# direction and too tight in another: it admitted ap-east-1, which 400s at
# call time, while missing ap-southeast-5/6/7 and ap-east-2, which are valid).
# AWS adds Bedrock regions a few times a year; re-check the source above and
# widen this set if a real Bedrock region is rejected here.
_AWS_REGIONS = frozenset(
    {
        "us-east-1",
        "us-east-2",
        "us-west-1",
        "us-west-2",
        "ca-central-1",
        "ca-west-1",
        "eu-central-1",
        "eu-central-2",
        "eu-north-1",
        "eu-south-1",
        "eu-south-2",
        "eu-west-1",
        "eu-west-2",
        "eu-west-3",
        "ap-east-2",
        "ap-northeast-1",
        "ap-northeast-2",
        "ap-northeast-3",
        "ap-south-1",
        "ap-south-2",
        "ap-southeast-1",
        "ap-southeast-2",
        "ap-southeast-3",
        "ap-southeast-4",
        "ap-southeast-5",
        "ap-southeast-6",
        "ap-southeast-7",
        "il-central-1",
        "me-central-1",
        "me-south-1",
        "af-south-1",
        "sa-east-1",
        "us-gov-east-1",
        "us-gov-west-1",
    }
)

# Opt-in trace-content flag. OFF unless the value is exactly "true" (case-insensitive,
# surrounding whitespace ignored) — see trace_content_enabled.
_TRACE_CONTENT_ENV = "LLM_TRACE_CONTENT"

# ...AND the deployment is a development one. "development" is the only non-production
# ENVIRONMENT value this repo uses — `app/config.py` defaults ENVIRONMENT to "production"
# and gates continuation-token keys on it, `app/authz.py` gates the dev internal-token
# path on it, and decision-service gates synthetic credit on it. Matching that vocabulary
# rather than inventing a second one ("staging", "test") keeps one answer to "is this a
# real deployment".
_TRACE_CONTENT_ENVIRONMENT = "development"


def _environment() -> str:
    """The deployment environment, read at call time.

    Read here rather than imported from `app.config` (which snapshots it at import) so it
    tracks the process environment the same way the flag does, and so a test can set both
    together.
    """
    return os.getenv("ENVIRONMENT", "production").strip().lower()


def trace_content_enabled() -> bool:
    """True when prompt/response CONTENT may be exported to LangSmith.

    Off by default, and deliberately not part of `LLMConfig`: the trace strippers in
    `client.py` / `transport.py` are module-level functions handed to `@traceable` at
    import time and receive only the traced call's arguments, so there is no config
    object to thread through them. Reading the environment at call time also means a
    test can flip the flag without rebuilding a client.

    **Only the exact string "true" enables it.** `1`, `yes`, `on`, and a typo all read as
    off. A flag that governs whether customer lending content leaves the building should
    fail closed on anything it does not positively recognize — the opposite convention
    (anything non-empty is true) turns `LLM_TRACE_CONTENT=false` into an export.

    **Requires ENVIRONMENT=development as well as the flag** (PR review). The prompt body
    keeps the business facts the model needs — `build_request` redacts identity PII but
    deliberately preserves loan amount, income, employment tenure, purpose and history —
    so this exports regulated customer lending content to a third-party telemetry vendor.
    A flag alone made that one stray environment variable away in production, with a
    startup warning as the only protection; a warning is a record of an incident, not a
    control. ENVIRONMENT defaults to "production", so the gate is closed unless a
    deployment positively declares itself a development one.

    `load_llm_config` additionally REFUSES TO BOOT when the flag is set outside
    development, so the misconfiguration is loud rather than silently ignored. This
    function still re-checks the environment instead of trusting that: it is called per
    trace, from processes and tests that may never have called `load_llm_config`, and the
    export is the thing that must fail closed.
    """
    return (
        os.getenv(_TRACE_CONTENT_ENV, "").strip().lower() == "true"
        and _environment() == _TRACE_CONTENT_ENVIRONMENT
    )


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
    timeout: float = 30.0  # seconds, enforced on every call
    max_retries: int = 3  # attempts for transient (429/5xx) failures
    max_tokens: int = 1024  # response cap sent to the provider
    temperature: float = 0.0  # deterministic summaries
    token_budget: int = 20_000  # per-request ceiling; refuse if exceeded
    aws_region: str | None = None  # bedrock only; pinned by _aws_region()

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


def _aws_region(provider: str) -> str | None:
    """Resolve the Bedrock region: AWS_REGION if set, else the pinned literal.

    Returns None off the bedrock path — `aws_region` is bedrock-only, and carrying
    a region on the direct-Anthropic route would report a region no call used.
    A malformed value raises rather than reaching the SDK, because a region that
    does not resolve fails at call time, mid-demo, instead of at boot.
    """
    if provider != "bedrock":
        return None
    raw = os.getenv("AWS_REGION", "").strip()
    if not raw:
        return _DEFAULT_BEDROCK_REGION
    if raw not in _AWS_REGIONS:
        raise LLMConfigError(
            f"AWS_REGION={raw!r} is not a known AWS region (expected e.g. "
            f"{_DEFAULT_BEDROCK_REGION!r}). Bedrock model access is granted per "
            "region, so a value that merely looks region-shaped fails at boot "
            "instead of reaching the SDK. Unset it to use the pinned default."
        )
    return raw


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
    # Refuse to boot when the trace-content flag is set outside a development deployment
    # (PR review). Silently ignoring it would leave an operator believing they had tracing
    # content while the export was closed; honouring it would turn regulated lending
    # content into third-party telemetry on a single stray variable. Fail loud, in the
    # same place this function already rejects a missing key and out-of-range numerics.
    if (
        os.getenv(_TRACE_CONTENT_ENV, "").strip().lower() == "true"
        and _environment() != _TRACE_CONTENT_ENVIRONMENT
    ):
        raise LLMConfigError(
            f"{_TRACE_CONTENT_ENV}=true requires ENVIRONMENT={_TRACE_CONTENT_ENVIRONMENT} "
            f"(got {_environment()!r}). It exports prompt and response content — loan "
            "amount, income, employment tenure, purpose — to LangSmith, and is not "
            "permitted outside a development deployment. Unset it, or set "
            f"ENVIRONMENT={_TRACE_CONTENT_ENVIRONMENT} if this really is one."
        )

    if trace_content_enabled():
        # Loud at boot, once, on the redacting logger. A deploy that exports customer
        # lending content to a third party must not do so silently — the whole point of
        # the flag is that someone chose it, so the log line is the evidence they did.
        get_llm_logger().warning(
            "%s=true: prompt and response CONTENT will be exported to LangSmith. "
            "Non-production only; use synthetic applicants.",
            _TRACE_CONTENT_ENV,
        )

    return LLMConfig(
        api_key=api_key,
        provider=provider,
        model=os.getenv("CLAUDE_MODEL", default_model),
        timeout=timeout,
        max_retries=max_retries,
        max_tokens=max_tokens,
        temperature=temperature,
        token_budget=token_budget,
        aws_region=_aws_region(provider),
    )
