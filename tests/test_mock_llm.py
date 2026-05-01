"""Tests for the deterministic mock LLM provider (PRD NFR-7)."""

from __future__ import annotations

import socket

import pytest

from epublate.errors import LLMResponseError
from epublate.llm import LLMProvider, Message, MockLLMProvider


def _msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)  # type: ignore[arg-type]


def test_mock_provider_implements_protocol() -> None:
    provider = MockLLMProvider()
    assert isinstance(provider, LLMProvider)


def test_set_response_replays_for_every_call() -> None:
    provider = MockLLMProvider()
    provider.set_response("hola")
    first = provider.chat([_msg("user", "hello")], model="gpt-mock")
    second = provider.chat([_msg("user", "hi")], model="gpt-mock")
    assert first.content == "hola"
    assert second.content == "hola"
    assert provider.call_count == 2


def test_queue_responses_pops_in_order() -> None:
    provider = MockLLMProvider()
    provider.queue_responses(["one", "two", "three"])
    contents = [
        provider.chat([_msg("user", str(i))], model="m").content for i in range(3)
    ]
    assert contents == ["one", "two", "three"]


def test_responder_callback_receives_request() -> None:
    provider = MockLLMProvider()
    provider.set_responder(lambda msgs, model: f"{model}:{msgs[-1].content}")
    result = provider.chat([_msg("user", "ping")], model="gpt-mock")
    assert result.content == "gpt-mock:ping"


def test_token_counts_are_deterministic() -> None:
    provider = MockLLMProvider()
    provider.set_response("aaaaaaaa")  # 8 chars -> 2 tokens
    out = provider.chat([_msg("user", "bbbbbbbb")], model="m")
    assert out.prompt_tokens == 2
    assert out.completion_tokens == 2
    assert out.total_tokens == 4
    assert out.model == "m"


def test_unset_provider_raises_typed_error() -> None:
    provider = MockLLMProvider()
    with pytest.raises(LLMResponseError):
        provider.chat([_msg("user", "x")], model="m")


def test_last_request_records_temperature_and_seed() -> None:
    provider = MockLLMProvider()
    provider.set_response("ok")
    provider.chat(
        [_msg("system", "be brief"), _msg("user", "yo")],
        model="gpt-mock",
        temperature=0.0,
        seed=7,
    )
    assert provider.last_request is not None
    assert provider.last_request.temperature == 0.0
    assert provider.last_request.seed == 7
    assert provider.last_request.messages[-1].content == "yo"


def test_provider_does_not_open_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-suspenders: prove the mock works with sockets disabled."""

    def _no_sockets(*_args: object, **_kwargs: object) -> socket.socket:
        raise RuntimeError("network is forbidden in tests")

    monkeypatch.setattr(socket, "socket", _no_sockets)
    provider = MockLLMProvider()
    provider.set_response("offline-ok")
    assert provider.chat([_msg("user", "x")], model="m").content == "offline-ok"
