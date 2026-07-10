"""Prompt library (turn-in #3).

Prompts live here as named, versioned templates — never as inline strings at the
call site. The request builder pulls a template by name and renders it with
per-request variables. Keeping prompts in one place means they can be reviewed,
diffed, and reused without hunting through business code.

    from app.prompts import get_prompt
    tmpl = get_prompt("loan_application_summary")
    system = tmpl.system
    user = tmpl.render_user(application_json=...)

Register a new prompt by defining a `PromptTemplate` in its own module and
calling `register()` (see `loan_summary.py`).
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class PromptTemplate:
    """A named, versioned prompt.

    - `system`: the system instruction (role, rules, guardrails).
    - `user_template`: a `str.format`-style template rendered per request.
    - `required_vars`: variable names that MUST be supplied to `render_user`.
    - `json_vars`: variable names whose value is a JSON document that must be
      redacted JSON-aware (`redact_json`) BEFORE it is rendered into the prompt.
      The generic build path applies this to every caller of `complete()`, so
      label-only identifiers (name/DOB/address/EIN/employer) cannot slip past
      the pattern redactor regardless of how the client is called.
    - `examples`: optional few-shot pairs `[{"user": ..., "assistant": ...}]`.
    - `output_schema`: optional JSON schema the response must satisfy (drives the
      validator). None means free text.
    """

    name: str
    version: str
    system: str
    user_template: str
    required_vars: tuple = ()
    json_vars: tuple = ()
    examples: list = field(default_factory=list)
    output_schema: Optional[dict] = None

    def render_user(self, **variables) -> str:
        """Render the user message. Raise KeyError if a required var is missing."""
        missing = [v for v in self.required_vars if v not in variables]
        if missing:
            raise KeyError(f"prompt {self.name!r} missing vars: {missing}")
        return self.user_template.format(**variables)


_REGISTRY: dict = {}


def register(template: PromptTemplate) -> None:
    """Add a template to the library. Raises on duplicate name."""
    if template.name in _REGISTRY:
        raise ValueError(f"prompt {template.name!r} already registered")
    _REGISTRY[template.name] = template


def get_prompt(name: str) -> PromptTemplate:
    """Fetch a template by name. Raises KeyError if unknown."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(
            f"unknown prompt {name!r}; registered: {sorted(_REGISTRY)}"
        )


def list_prompts() -> list:
    """Names of all registered prompts."""
    return sorted(_REGISTRY)


# Register built-in prompts at import time.
from . import loan_summary  # noqa: E402,F401  (side effect: registers templates)
