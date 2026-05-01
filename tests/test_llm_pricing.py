"""Tests for the per-model pricing table (PRD F-LLM-7)."""

from __future__ import annotations

import pytest

from epublate.llm.pricing import (
    ModelPrice,
    estimate_cost,
    get_price,
    reset_prices,
    set_price,
)


@pytest.fixture(autouse=True)
def _isolate_price_table() -> None:
    """Each test sees the package-default table."""

    reset_prices()
    yield
    reset_prices()


def test_known_model_cost_matches_table() -> None:
    cost = estimate_cost("gpt-5-mini", prompt_tokens=1_000_000, completion_tokens=0)
    assert cost == pytest.approx(0.15)


def test_completion_tokens_priced_separately() -> None:
    cost = estimate_cost("gpt-4o", prompt_tokens=0, completion_tokens=1_000_000)
    assert cost == pytest.approx(10.00)


def test_unknown_model_falls_back_to_zero() -> None:
    assert estimate_cost("ollama/llama3", 1000, 1000) == 0.0


def test_set_price_overrides_table() -> None:
    set_price("custom-model", ModelPrice(input_per_mtok=1.0, output_per_mtok=2.0))
    assert estimate_cost("custom-model", 1_000_000, 0) == pytest.approx(1.0)
    assert estimate_cost("custom-model", 0, 1_000_000) == pytest.approx(2.0)


def test_reset_prices_restores_defaults() -> None:
    set_price("gpt-5-mini", ModelPrice(input_per_mtok=999.0, output_per_mtok=999.0))
    reset_prices()
    price = get_price("gpt-5-mini")
    assert price.input_per_mtok == pytest.approx(0.15)
    assert price.output_per_mtok == pytest.approx(0.60)


def test_negative_tokens_rejected() -> None:
    with pytest.raises(ValueError):
        estimate_cost("gpt-5-mini", -1, 0)


def test_mock_model_is_free() -> None:
    assert estimate_cost("mock", 10_000, 10_000) == 0.0
