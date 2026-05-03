"""Unit tests for :mod:`epublate.llm.json_mode`.

These pin the contract of :func:`chat_with_json_fallback`:

* On success or any non-``response_format`` error, the helper is a
  pass-through.
* On an :class:`LLMResponseError` whose message indicates the endpoint
  rejected our ``response_format`` hint (Groq's ``json_validate_failed``,
  generic "response_format" mentions, etc.), the helper retries once
  *without* ``response_format`` and returns the second result.
* The helper never retries when ``response_format`` was ``None`` to
  begin with — the original error propagates so the caller's audit
  trail records the real cause.
"""

from __future__ import annotations

import logging

import pytest

from epublate.errors import LLMResponseError, LLMTransportError
from epublate.llm.base import Message, ResponseFormat
from epublate.llm.json_mode import chat_with_json_fallback
from epublate.llm.mock import MockLLMProvider


def _msgs(text: str = "hello") -> list[Message]:
    return [
        Message(role="system", content="system"),
        Message(role="user", content=text),
    ]


def test_pass_through_on_success() -> None:
    provider = MockLLMProvider()
    provider.set_response('{"entities": []}')

    result = chat_with_json_fallback(
        provider,
        _msgs(),
        model="gpt-mock",
        response_format=ResponseFormat(type="json_object"),
        temperature=0.0,
        seed=7,
    )

    assert result.content == '{"entities": []}'
    assert provider.call_count == 1
    last = provider.last_request
    assert last is not None
    assert last.response_format == ResponseFormat(type="json_object")


def test_retries_without_response_format_on_groq_json_validate_failed(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Groq's ``json_validate_failed`` 400 on reasoning models — the
    canonical bug that prompted this helper. The wrapper retries once
    without ``response_format`` and returns the recovery payload.
    """

    provider = MockLLMProvider()
    captured_models: list[ResponseFormat | None] = []

    def _responder(messages: list[Message], _model: str) -> str:
        # Mock provider records the response_format on every call; we
        # peek at the recorded calls to drive the responder's outcome
        # so the second attempt (without response_format) returns
        # valid JSON instead of raising.
        last = provider.calls[-1]
        captured_models.append(last.response_format)
        if last.response_format is not None:
            raise LLMResponseError(
                "OpenAI API status 400: Error code: 400 - {'error': "
                "{'message': 'litellm.BadRequestError: GroqException - "
                '{"error":{"message":"Failed to validate JSON. Please '
                "adjust your prompt. See 'failed_generation' for more "
                'details.","type":"invalid_request_error","code":'
                '"json_validate_failed","failed_generation":""}}\\n"}}'
            )
        return '{"entities": [{"type": "character", "source": "Geralt"}]}'

    provider.set_responder(_responder)

    with caplog.at_level(logging.WARNING, logger="epublate.llm.json_mode"):
        result = chat_with_json_fallback(
            provider,
            _msgs(),
            model="gpt-oss-20b",
            response_format=ResponseFormat(type="json_object"),
            temperature=0.0,
            seed=7,
        )

    assert provider.call_count == 2
    assert captured_models == [ResponseFormat(type="json_object"), None]
    assert "Geralt" in result.content
    # The warning is emitted exactly once and names the rejection.
    matches = [r for r in caplog.records if "JSON-mode rejected" in r.message]
    assert len(matches) == 1


def test_retries_when_message_mentions_response_format(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Endpoints that 400 with ``response_format not supported`` (or
    similar phrasing) should also trigger the fallback."""

    provider = MockLLMProvider()
    attempts = {"n": 0}

    def _responder(messages: list[Message], _model: str) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise LLMResponseError(
                "OpenAI API status 400: response_format not supported by this endpoint"
            )
        return '{"entities": []}'

    provider.set_responder(_responder)

    with caplog.at_level(logging.WARNING, logger="epublate.llm.json_mode"):
        result = chat_with_json_fallback(
            provider,
            _msgs(),
            model="gpt-mock",
            response_format=ResponseFormat(type="json_object"),
        )

    assert result.content == '{"entities": []}'
    assert provider.call_count == 2


def test_no_retry_when_response_format_is_none() -> None:
    """If the caller didn't ask for JSON mode, we never retry —
    any error is theirs to handle and shouldn't double-charge them."""

    provider = MockLLMProvider()

    def _responder(messages: list[Message], _model: str) -> str:
        raise LLMResponseError("OpenAI API status 400: response_format invalid")

    provider.set_responder(_responder)

    with pytest.raises(LLMResponseError):
        chat_with_json_fallback(
            provider,
            _msgs(),
            model="gpt-mock",
            response_format=None,
        )

    assert provider.call_count == 1


def test_no_retry_on_unrelated_400() -> None:
    """A genuine prompt-shape 400 (no JSON-mode signal) propagates so
    the curator sees the real error in the Inbox / log."""

    provider = MockLLMProvider()

    def _responder(messages: list[Message], _model: str) -> str:
        raise LLMResponseError("OpenAI API status 400: messages cannot be empty")

    provider.set_responder(_responder)

    with pytest.raises(LLMResponseError, match="messages cannot be empty"):
        chat_with_json_fallback(
            provider,
            _msgs(),
            model="gpt-mock",
            response_format=ResponseFormat(type="json_object"),
        )

    assert provider.call_count == 1


def test_transport_errors_are_not_retried() -> None:
    """A network-layer failure isn't a JSON-mode problem; the wrapper
    must let :class:`LLMTransportError` through unchanged so the upper
    retry policy (in the OpenAI-compat provider) is the only place
    transport retries happen."""

    provider = MockLLMProvider()

    def _responder(messages: list[Message], _model: str) -> str:
        raise LLMTransportError("connection reset")

    provider.set_responder(_responder)

    with pytest.raises(LLMTransportError):
        chat_with_json_fallback(
            provider,
            _msgs(),
            model="gpt-mock",
            response_format=ResponseFormat(type="json_object"),
        )

    assert provider.call_count == 1


def test_long_error_message_is_truncated_in_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LiteLLM dumps its full fallback model list on every error; the
    log line truncates so the Inbox stays readable."""

    provider = MockLLMProvider()
    huge = "a" * 5000
    attempts = {"n": 0}

    def _responder(messages: list[Message], _model: str) -> str:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise LLMResponseError(f"json_validate_failed {huge}")
        return '{"entities": []}'

    provider.set_responder(_responder)

    with caplog.at_level(logging.WARNING, logger="epublate.llm.json_mode"):
        chat_with_json_fallback(
            provider,
            _msgs(),
            model="gpt-mock",
            response_format=ResponseFormat(type="json_object"),
        )

    matches = [r for r in caplog.records if "JSON-mode rejected" in r.message]
    assert len(matches) == 1
    formatted = matches[0].getMessage()
    # The truncation cap (240 chars) keeps the formatted log line bounded.
    assert len(formatted) < 600
    assert formatted.endswith("\u2026")
