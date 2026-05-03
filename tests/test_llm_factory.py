"""Tests for the LLM provider factory."""

from __future__ import annotations

import pytest

from epublate.errors import ConfigurationError
from epublate.llm.factory import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_HELPER_MODEL,
    ENV_MODEL,
    ENV_PROVIDER,
    ENV_REASONING_EFFORT,
    build_provider,
    resolve_helper_model,
)
from epublate.llm.mock import MockLLMProvider
from epublate.llm.openai_compat import OpenAICompatProvider


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        ENV_PROVIDER,
        ENV_BASE_URL,
        ENV_API_KEY,
        ENV_MODEL,
        ENV_HELPER_MODEL,
        ENV_REASONING_EFFORT,
    ):
        monkeypatch.delenv(var, raising=False)


def test_explicit_mock_flag_wins() -> None:
    provider = build_provider(mock=True)
    assert isinstance(provider, MockLLMProvider)


def test_env_mock_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_PROVIDER, "mock")
    provider = build_provider()
    assert isinstance(provider, MockLLMProvider)


def test_real_provider_requires_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_API_KEY, "sk-test")
    with pytest.raises(ConfigurationError):
        build_provider()


def test_real_provider_built_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_BASE_URL, "https://example.invalid/v1")
    monkeypatch.setenv(ENV_API_KEY, "sk-test")
    monkeypatch.setenv(ENV_MODEL, "gpt-5-mini")
    provider = build_provider()
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.default_model == "gpt-5-mini"
    assert provider.api_key == "sk-test"
    assert provider.reasoning_effort is None


def test_real_provider_picks_up_reasoning_effort_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``EPUBLATE_LLM_REASONING_EFFORT`` lets curators tune reasoning
    models for speed without code changes — the factory plumbs it
    onto the provider so every chat call carries the field."""

    monkeypatch.setenv(ENV_BASE_URL, "https://example.invalid/v1")
    monkeypatch.setenv(ENV_API_KEY, "sk-test")
    monkeypatch.setenv(ENV_MODEL, "gpt-oss-20b")
    monkeypatch.setenv(ENV_REASONING_EFFORT, "low")
    provider = build_provider()
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.reasoning_effort == "low"


def test_project_override_beats_env_for_reasoning_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Per-project overrides win over the env var so a curator can run
    one project on ``high`` (translation quality matters) while
    another runs on ``low`` (speed matters), using the same env."""

    monkeypatch.setenv(ENV_BASE_URL, "https://example.invalid/v1")
    monkeypatch.setenv(ENV_API_KEY, "sk-test")
    monkeypatch.setenv(ENV_MODEL, "gpt-oss-20b")
    monkeypatch.setenv(ENV_REASONING_EFFORT, "low")
    provider = build_provider(overrides={"reasoning_effort": "high"})
    assert isinstance(provider, OpenAICompatProvider)
    assert provider.reasoning_effort == "high"


def test_resolve_helper_model_explicit_override_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_HELPER_MODEL, "from-env")
    assert resolve_helper_model("translator", override="explicit") == "explicit"


def test_resolve_helper_model_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_HELPER_MODEL, "cheap-helper")
    assert resolve_helper_model("translator") == "cheap-helper"


def test_resolve_helper_model_falls_back_to_translator() -> None:
    assert resolve_helper_model("gpt-5-mini") == "gpt-5-mini"


def test_resolve_helper_model_strips_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_HELPER_MODEL, "  spaced  ")
    assert resolve_helper_model(None) == "spaced"


def test_resolve_helper_model_raises_when_nothing_set() -> None:
    with pytest.raises(ConfigurationError):
        resolve_helper_model(None)
    with pytest.raises(ConfigurationError):
        resolve_helper_model("   ")
