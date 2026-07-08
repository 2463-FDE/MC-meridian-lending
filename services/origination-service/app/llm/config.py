"""Concern 1 — Config.

Model id, default params, and timeout live here in one place. The API key is
loaded from the environment only (never hardcoded, unlike the bureau keys in
`app/config.py`). `load_llm_config()` fails loud at boot if the key is missing,
so a misconfigured deploy dies at startup instead of on the first customer call.
"""
import os
from dataclasses import dataclass, field

from .errors import LLMConfigError

# Haiku 4.5 — fastest/cheapest, appropriate for loan summarization (ADR 0005).
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


@dataclass(frozen=True)
class LLMConfig:
    """Immutable client configuration. Build via `load_llm_config()`.

    The credential (`api_key`) is kept out of the default repr/str
    (`repr=False`) so it cannot leak via `log.info(config)`, an exception that
    dumps locals, or a traceback. The redactor does NOT catch API keys (it
    targets PII patterns), so keeping the secret out of every string
    representation is the guardrail. Log via `redacted()` only.
    """

    api_key: str = field(repr=False)
    model: str = _DEFAULT_MODEL
    timeout: float = 30.0            # seconds, enforced on every call
    max_retries: int = 3            # attempts for transient (429/5xx) failures
    max_tokens: int = 1024          # response cap sent to the provider
    temperature: float = 0.0        # deterministic summaries
    token_budget: int = 20_000      # per-request ceiling; refuse if exceeded

    def redacted(self) -> dict:
        """Config safe to log — never includes the credential."""
        return {
            "model": self.model,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "token_budget": self.token_budget,
        }

    def __str__(self) -> str:  # never render the secret, even via str()
        return f"LLMConfig({self.redacted()})"


def load_llm_config() -> LLMConfig:
    """Load config from the environment. Raise `LLMConfigError` if the key is missing.

    Call this at application startup (fail loud at boot) — not lazily on first use.
    """
    api_key = os.getenv("CLAUDE_API_KEY")
    if not api_key:
        raise LLMConfigError(
            "CLAUDE_API_KEY is not set. The LLM client cannot start without it. "
            "Set it in the environment (never hardcode it)."
        )

    def _num(env: str, default, cast):
        raw = os.getenv(env)
        if raw is None:
            return default
        try:
            return cast(raw)
        except ValueError:
            raise LLMConfigError(f"{env}={raw!r} is not a valid {cast.__name__}.")

    return LLMConfig(
        api_key=api_key,
        model=os.getenv("CLAUDE_MODEL", _DEFAULT_MODEL),
        timeout=_num("CLAUDE_TIMEOUT", 30.0, float),
        max_retries=_num("CLAUDE_MAX_RETRIES", 3, int),
        max_tokens=_num("CLAUDE_MAX_TOKENS", 1024, int),
        temperature=_num("CLAUDE_TEMPERATURE", 0.0, float),
        token_budget=_num("CLAUDE_TOKEN_BUDGET", 20_000, int),
    )
