"""Hardened LLM client for Claude API integration (ADR 0005).

Public surface:
    from app.llm import ClaudeClient, load_llm_config
    from app.llm import ModelAdapter, ClaudeAdapter, FakeAdapter

The client is decomposed into seven collaborators (one per ADR-0005 checklist
concern): config, adapter, request builder, transport, streaming, validator,
logger. `ClaudeClient` wires them together; the adapter is injected so tests run
against `FakeAdapter` and spend no tokens.
"""
from .adapter import BedrockAdapter, ClaudeAdapter, Completion, FakeAdapter, ModelAdapter
from .client import ClaudeClient
from .config import LLMConfig, load_llm_config
from .errors import (
    LLMConfigError,
    LLMError,
    LLMHTTPError,
    LLMTimeoutError,
    TokenBudgetExceeded,
    ValidationFailed,
)

__all__ = [
    "ClaudeClient",
    "LLMConfig",
    "load_llm_config",
    "ModelAdapter",
    "ClaudeAdapter",
    "BedrockAdapter",
    "FakeAdapter",
    "Completion",
    "LLMError",
    "LLMConfigError",
    "LLMHTTPError",
    "LLMTimeoutError",
    "TokenBudgetExceeded",
    "ValidationFailed",
]
