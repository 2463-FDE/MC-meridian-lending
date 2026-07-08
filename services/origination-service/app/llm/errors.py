"""Exception hierarchy for the LLM client.

Every failure the client can raise is a subclass of `LLMError`, so callers can
catch one type. Messages are safe to log (no PII, no API key) by construction —
they describe the failure mode, never echo request content.
"""


class LLMError(Exception):
    """Base class for all LLM client errors."""


class LLMConfigError(LLMError):
    """Configuration is invalid or incomplete (e.g. missing API key at boot)."""


class TokenBudgetExceeded(LLMError):
    """Estimated request+response tokens exceed the per-request budget.

    Raised pre-flight, before any network call, so an oversized prompt costs
    nothing.
    """


class LLMTimeoutError(LLMError):
    """The provider did not respond within the configured timeout."""


class LLMHTTPError(LLMError):
    """The provider returned an HTTP error.

    `status_code` is the HTTP status. `retryable` is True for 429/5xx (transient)
    and False for 4xx (caller must fix the request).
    """

    def __init__(self, message: str, status_code: int, retryable: bool):
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class ValidationFailed(LLMError):
    """Model output failed schema validation or a content/length/leak guard."""
