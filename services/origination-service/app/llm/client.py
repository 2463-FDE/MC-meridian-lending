"""`ClaudeClient` — wires the seven collaborators into one call.

Flow for a structured completion:
    build request (concern 3, incl. cost guard)
      -> transport with timeout+retry (concern 4)
      -> validate + guard output (concern 6)
      -> log metrics, redacted (concern 7)

The adapter (concern 2) is injected, so tests pass `FakeAdapter` and spend no
tokens. Config (concern 1) is passed in, built via `load_llm_config()` at boot.

The client never logs the API key or raw request/response content (which carries
customer PII). It logs metrics only, and every log line additionally passes
through the service's redacting formatter as defense in depth.
"""
from __future__ import annotations

import uuid
from time import perf_counter
from typing import Any, Iterator

from ..prompts import get_prompt
from .adapter import ClaudeAdapter, ModelAdapter
from .config import LLMConfig
from .errors import LLMError, ValidationFailed
from .logging_setup import get_llm_logger
from .request_builder import build_request
from .transport import call_with_retry
from .validator import guard_output, validate_structured

_UNSET = object()


class ClaudeClient:
    """Hardened Claude client. Build with `ClaudeClient(load_llm_config())`."""

    def __init__(self, config: LLMConfig, adapter: ModelAdapter | None = None):
        self.config = config
        self.adapter = adapter if adapter is not None else ClaudeAdapter(config.api_key)
        self.log = get_llm_logger()

    def complete(
        self,
        prompt_name: str,
        *,
        history: list[dict] | None = None,
        idempotency_key: str | None = None,
        fallback: Any = _UNSET,
        **variables,
    ) -> Any:
        """Run a prompt end-to-end and return validated output.

        For a prompt with an `output_schema`, returns the parsed/validated dict.
        For a free-text prompt, returns the guarded string.

        `fallback`: if given, returned instead of raising when the model output
        fails validation/guards (never returns malformed output either way).
        Transport and budget errors always raise — a fallback would mask them.
        """
        template = get_prompt(prompt_name)
        request_id = idempotency_key or uuid.uuid4().hex

        # Concern 3: build + cost guard (raises TokenBudgetExceeded before network).
        built = build_request(
            template,
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            timeout=self.config.timeout,
            token_budget=self.config.token_budget,
            history=history,
            idempotency_key=request_id,
            **variables,
        )

        # Concern 4: transport with timeout + bounded retry.
        retries = {"n": 0}

        def _on_retry(attempt, delay, exc):
            retries["n"] = attempt
            self.log.warning(
                "llm retry attempt=%d delay=%.2fs reason=%s request_id=%s",
                attempt, delay, type(exc).__name__, request_id,
            )

        t0 = perf_counter()
        try:
            completion = call_with_retry(
                self.adapter,
                built.request,
                max_retries=self.config.max_retries,
                on_retry=_on_retry,
            )
        except LLMError as exc:
            self.log.error(
                "llm call failed error=%s request_id=%s retries=%d",
                type(exc).__name__, request_id, retries["n"],
            )
            raise
        latency_ms = (perf_counter() - t0) * 1000

        # Concern 6: validate + guard. Never pass malformed output forward.
        try:
            if template.output_schema:
                result = validate_structured(completion.text, template.output_schema)
            else:
                guard_output(completion.text)
                result = completion.text
        except ValidationFailed as exc:
            self.log.warning(
                "llm output rejected error=%s request_id=%s", exc, request_id,
            )
            if fallback is not _UNSET:
                return fallback
            raise

        # Concern 7: metrics only — no key, no raw content. (Formatter also redacts.)
        self.log.info(
            "llm ok request_id=%s prompt=%s v=%s model=%s latency_ms=%.0f "
            "input_tokens=%d output_tokens=%d est_input_tokens=%d "
            "trimmed_history=%d retries=%d",
            request_id, template.name, template.version, completion.model,
            latency_ms, completion.input_tokens, completion.output_tokens,
            built.estimated_input_tokens, built.trimmed_history_turns, retries["n"],
        )
        return result

    def summarize_application(self, application_json: str, **kwargs) -> dict:
        """Convenience wrapper for the loan-summary prompt."""
        return self.complete(
            "loan_application_summary",
            application_json=application_json,
            **kwargs,
        )

    def stream(self, prompt_name: str, *, idempotency_key: str | None = None,
               **variables) -> Iterator[str]:
        """Stream text chunks (concern 5). Deferred from the Week-1 product path.

        No retry/validation wrapping yet — buffer the chunks and validate before
        use when this is wired into a UI (see ADR 0005 revision).
        """
        template = get_prompt(prompt_name)
        request_id = idempotency_key or uuid.uuid4().hex
        built = build_request(
            template,
            model=self.config.model,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
            timeout=self.config.timeout,
            token_budget=self.config.token_budget,
            idempotency_key=request_id,
            **variables,
        )
        self.log.info("llm stream start request_id=%s prompt=%s",
                      request_id, template.name)
        yield from self.adapter.stream(built.request)
