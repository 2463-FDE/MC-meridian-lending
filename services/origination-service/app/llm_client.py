"""Compat shim: Week 1 spec AC#1 imports `from app.llm_client import ClaudeClient`.

The real implementation lives in the app.llm package (client/adapter/transport/…);
this module re-exports the public surface so the frozen acceptance contract holds.
"""
from .llm import ClaudeClient, load_llm_config

__all__ = ["ClaudeClient", "load_llm_config"]
