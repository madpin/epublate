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
    build_provider,
    resolve_helper_model,
)
from epublate.llm.mock import MockLLMProvider
from epublate.llm.openai_compat import OpenAICompatProvider


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (ENV_PROVIDER, ENV_BASE_URL, ENV_API_KEY, ENV_MODEL, ENV_HELPER_MODEL):
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
