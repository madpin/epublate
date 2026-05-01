"""Build the active :class:`LLMProvider` from environment + flags.

Resolution order (per call):

1. ``mock=True`` — pin the deterministic mock provider regardless of env.
2. ``EPUBLATE_LLM=mock`` — same effect; this is the env knob the
   ``--mock-llm`` CLI flag sets in :mod:`epublate.cli`.
3. Otherwise build :class:`OpenAICompatProvider` from
   ``EPUBLATE_LLM_BASE_URL`` / ``EPUBLATE_LLM_API_KEY`` / ``EPUBLATE_LLM_MODEL``
   plus the optional ``EPUBLATE_LLM_ORG``.

Persisting model choice in the project DB is M3+ (lands with the project
settings table). Until then this factory is the single point that turns
external configuration into a provider instance.

The helper-model slot (PRD F-LLM-2) is resolved by
:func:`resolve_helper_model`: it reads ``EPUBLATE_LLM_HELPER_MODEL`` if
set, otherwise falls back to the translator model so the cheapest
default is "use the same endpoint twice". The helper model is wired
through :mod:`epublate.core.extractor` for book intake (M5) and
batch pre-pass.
"""

from __future__ import annotations

import os

from epublate.errors import ConfigurationError
from epublate.llm.base import LLMProvider
from epublate.llm.mock import MockLLMProvider
from epublate.llm.openai_compat import OpenAICompatProvider

ENV_PROVIDER = "EPUBLATE_LLM"
ENV_BASE_URL = "EPUBLATE_LLM_BASE_URL"
ENV_API_KEY = "EPUBLATE_LLM_API_KEY"
ENV_MODEL = "EPUBLATE_LLM_MODEL"
ENV_HELPER_MODEL = "EPUBLATE_LLM_HELPER_MODEL"
ENV_ORG = "EPUBLATE_LLM_ORG"


def build_provider(*, mock: bool = False) -> LLMProvider:
    """Construct the active provider from the environment.

    Raises :class:`ConfigurationError` when a real provider is requested
    but ``EPUBLATE_LLM_MODEL`` is missing — the model slot is mandatory
    even when the endpoint is anonymous (PRD F-LLM-1).
    """

    if mock or os.environ.get(ENV_PROVIDER, "").strip().lower() == "mock":
        return MockLLMProvider()

    model = os.environ.get(ENV_MODEL, "").strip()
    if not model:
        raise ConfigurationError(
            f"set {ENV_MODEL} (and optionally {ENV_BASE_URL} / {ENV_API_KEY}) "
            "or pass --mock-llm to use the deterministic mock provider"
        )

    return OpenAICompatProvider(
        base_url=os.environ.get(ENV_BASE_URL) or None,
        api_key=os.environ.get(ENV_API_KEY, ""),
        default_model=model,
        organization=os.environ.get(ENV_ORG) or None,
    )


def resolve_helper_model(
    translator_model: str | None,
    *,
    override: str | None = None,
) -> str:
    """Resolve the helper-model slug for a given translator model.

    Resolution order:

    1. Explicit ``override`` argument (CLI flag wins).
    2. ``EPUBLATE_LLM_HELPER_MODEL`` env var.
    3. ``translator_model`` — F-LLM-2 explicitly says the helper can be
       the same as the translator, so this is the cheapest default
       when the user hasn't picked a separate cheap model.

    Raises :class:`ConfigurationError` if every source is empty.
    """

    if override and override.strip():
        return override.strip()
    env = os.environ.get(ENV_HELPER_MODEL, "").strip()
    if env:
        return env
    if translator_model and translator_model.strip():
        return translator_model.strip()
    raise ConfigurationError(
        f"no helper model: pass --helper-model, set {ENV_HELPER_MODEL}, "
        "or provide a translator model to fall back on"
    )


__all__ = [
    "ENV_API_KEY",
    "ENV_BASE_URL",
    "ENV_HELPER_MODEL",
    "ENV_MODEL",
    "ENV_ORG",
    "ENV_PROVIDER",
    "build_provider",
    "resolve_helper_model",
]
