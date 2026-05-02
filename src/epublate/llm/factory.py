"""Build the active :class:`LLMProvider` from environment + flags.

Resolution order (per call):

1. ``mock=True`` — pin the deterministic mock provider regardless of env.
2. ``EPUBLATE_LLM=mock`` — same effect; this is the env knob the
   ``--mock-llm`` CLI flag sets in :mod:`epublate.cli`.
3. Otherwise build :class:`OpenAICompatProvider` from per-project
   overrides (``project.llm_overrides``) merged with the env
   defaults: ``EPUBLATE_LLM_BASE_URL`` / ``EPUBLATE_LLM_API_KEY`` /
   ``EPUBLATE_LLM_MODEL`` plus the optional ``EPUBLATE_LLM_ORG``.

The Settings → LLM panel persists per-project overrides as a JSON
blob on the ``project`` row (PRD §4.6 / M6). The API key intentionally
stays env-only — embedding it in a project DB would invite accidents
when sharing ``.epublate`` archives (invariant 5).

The helper-model slot (PRD F-LLM-2) is resolved by
:func:`resolve_helper_model`: it reads the explicit override if
provided, then ``EPUBLATE_LLM_HELPER_MODEL``, then falls back to the
translator model so the cheapest default is "use the same endpoint
twice". The helper model is wired through
:mod:`epublate.core.extractor` for book intake (M5) and batch pre-pass.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

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


def _override_str(overrides: Mapping[str, object] | None, key: str) -> str | None:
    """Return ``overrides[key]`` as a non-empty string, else ``None``.

    The repo deserializer returns ``object`` because the JSON column is
    untyped at the DB layer; we coerce defensively here so a stray
    ``None`` / ``""`` / ``42`` value never silently pollutes the
    provider configuration.
    """

    if overrides is None:
        return None
    raw = overrides.get(key)
    if raw is None:
        return None
    text = str(raw).strip()
    return text or None


def build_provider(
    *,
    mock: bool = False,
    overrides: Mapping[str, object] | None = None,
) -> LLMProvider:
    """Construct the active provider from env + optional per-project overrides.

    ``overrides`` may carry ``base_url`` / ``translator_model`` /
    ``helper_model``; when present they win over the matching env var.
    The ``helper_model`` override is read by :func:`resolve_helper_model`,
    not here, but the call sites pass the same dict to keep the
    resolution centralized.

    Raises :class:`ConfigurationError` when a real provider is requested
    but no model slot is resolvable — the model slot is mandatory even
    when the endpoint is anonymous (PRD F-LLM-1).
    """

    if mock or os.environ.get(ENV_PROVIDER, "").strip().lower() == "mock":
        return MockLLMProvider()

    base_url = _override_str(overrides, "base_url") or os.environ.get(ENV_BASE_URL)
    model = (
        _override_str(overrides, "translator_model")
        or os.environ.get(ENV_MODEL, "").strip()
    )
    if not model:
        raise ConfigurationError(
            f"set {ENV_MODEL} (and optionally {ENV_BASE_URL} / {ENV_API_KEY}), "
            "set a per-project override in Settings → LLM, "
            "or pass --mock-llm to use the deterministic mock provider"
        )

    return OpenAICompatProvider(
        base_url=base_url or None,
        api_key=os.environ.get(ENV_API_KEY, ""),
        default_model=model,
        organization=os.environ.get(ENV_ORG) or None,
    )


def resolve_helper_model(
    translator_model: str | None,
    *,
    override: str | None = None,
    project_overrides: Mapping[str, object] | None = None,
) -> str:
    """Resolve the helper-model slug for a given translator model.

    Resolution order:

    1. Explicit ``override`` argument (CLI flag wins).
    2. ``project_overrides["helper_model"]`` (Settings → LLM panel).
    3. ``EPUBLATE_LLM_HELPER_MODEL`` env var.
    4. ``translator_model`` — F-LLM-2 explicitly says the helper can be
       the same as the translator, so this is the cheapest default
       when the user hasn't picked a separate cheap model.

    Raises :class:`ConfigurationError` if every source is empty.
    """

    if override and override.strip():
        return override.strip()
    project_helper = _override_str(project_overrides, "helper_model")
    if project_helper:
        return project_helper
    env = os.environ.get(ENV_HELPER_MODEL, "").strip()
    if env:
        return env
    if translator_model and translator_model.strip():
        return translator_model.strip()
    raise ConfigurationError(
        f"no helper model: pass --helper-model, set {ENV_HELPER_MODEL}, "
        "or provide a translator model to fall back on"
    )


def resolve_translator_model(
    *,
    override: str | None = None,
    project_overrides: Mapping[str, object] | None = None,
    fallback: str | None = None,
) -> str:
    """Resolve the translator-model slug, reading the same precedence as build.

    Useful for screens that need the model name *before* building a
    provider (Reader, Inbox, Settings preview). Mirrors
    :func:`build_provider`'s precedence so the displayed model matches
    the one the worker will actually call.
    """

    if override and override.strip():
        return override.strip()
    project_model = _override_str(project_overrides, "translator_model")
    if project_model:
        return project_model
    env = os.environ.get(ENV_MODEL, "").strip()
    if env:
        return env
    if fallback and fallback.strip():
        return fallback.strip()
    raise ConfigurationError(
        f"no translator model: pass --model, set {ENV_MODEL}, or "
        "configure one in Settings → LLM"
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
    "resolve_translator_model",
]
