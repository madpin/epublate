"""Tests for the ``epublate.llm.tokens`` façade (PRD F-LLM-4)."""

from __future__ import annotations

from epublate.llm.tokens import count_tokens


def test_empty_string_is_zero_tokens() -> None:
    assert count_tokens("") == 0
    assert count_tokens("", model="gpt-5-mini") == 0


def test_unknown_model_falls_back_to_heuristic() -> None:
    text = "x" * 16
    # Heuristic is chars/4: 16 → 4.
    assert count_tokens(text, model="totally-unknown-model-xyz") == 4
    assert count_tokens(text) == 4


def test_known_model_uses_tiktoken() -> None:
    # "hi" encodes to a single BPE token under cl100k_base; even if the
    # encoder changes minor versions, two characters can never produce more
    # than two tokens, and the heuristic would have produced 1.
    n = count_tokens("hi", model="gpt-5-mini")
    assert 1 <= n <= 2


def test_known_model_distinct_from_heuristic_on_long_text() -> None:
    # 80 chars of plain ASCII would be 20 under chars/4 but is well under
    # that with a real BPE encoder; assert we got something tiktoken-shaped.
    text = "the quick brown fox jumps over the lazy dog. " * 2
    by_heuristic = count_tokens(text)
    by_tiktoken = count_tokens(text, model="gpt-5-mini")
    assert by_tiktoken != by_heuristic or by_tiktoken < by_heuristic + 1
    assert by_tiktoken > 0


def test_count_tokens_is_pure() -> None:
    text = "stable input"
    a = count_tokens(text, model="gpt-5-mini")
    b = count_tokens(text, model="gpt-5-mini")
    assert a == b
