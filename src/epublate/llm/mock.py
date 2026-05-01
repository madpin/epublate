"""Deterministic mock LLM provider.

Used by the test suite (PRD NFR-7) and by ``epublate --mock-llm`` in demos.
Crucially, this module **must not import any HTTP client** and must not open
sockets — the bootstrap contract requires ``uv run pytest`` to pass with no
network access.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from epublate.errors import LLMResponseError
from epublate.llm.base import ChatResult, Message, ResponseFormat


def _approx_token_count(text: str) -> int:
    """Cheap, deterministic token estimate.

    Real tokenization uses ``tiktoken`` and lands with the OpenAI-compatible
    provider in M2. The mock just needs to be deterministic and monotonically
    increasing with text length so tests can assert on it.
    """

    return max(1, len(text) // 4)


Responder = Callable[[list[Message], str], str]


@dataclass(slots=True)
class MockCall:
    """A single recorded call into the mock provider."""

    messages: tuple[Message, ...]
    model: str
    response_format: ResponseFormat | None
    temperature: float | None
    seed: int | None


@dataclass(slots=True)
class MockLLMProvider:
    """A scriptable, in-memory LLM provider.

    Three response modes, in priority order:

    1. ``queue_responses([...])`` — pop one per call, in FIFO order.
    2. ``set_response(...)`` — single fixed response replayed for every call.
    3. ``set_responder(fn)`` — programmatic responder ``(messages, model) -> str``.

    If none of those have been configured, ``chat()`` raises
    :class:`~epublate.errors.LLMResponseError` to make missing fixtures
    obvious in tests.
    """

    name: str = "mock"
    calls: list[MockCall] = field(default_factory=list)
    _queue: deque[str] = field(default_factory=deque)
    _fixed: str | None = None
    _responder: Responder | None = None

    def set_response(self, content: str) -> None:
        self._fixed = content
        self._responder = None
        self._queue.clear()

    def queue_responses(self, contents: Iterable[str]) -> None:
        self._queue.extend(contents)
        self._fixed = None
        self._responder = None

    def set_responder(self, responder: Responder) -> None:
        self._responder = responder
        self._fixed = None
        self._queue.clear()

    def reset(self) -> None:
        self.calls.clear()
        self._queue.clear()
        self._fixed = None
        self._responder = None

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_request(self) -> MockCall | None:
        return self.calls[-1] if self.calls else None

    def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        response_format: ResponseFormat | None = None,
        temperature: float | None = None,
        seed: int | None = None,
    ) -> ChatResult:
        self.calls.append(
            MockCall(
                messages=tuple(messages),
                model=model,
                response_format=response_format,
                temperature=temperature,
                seed=seed,
            )
        )
        content = self._next_response(messages, model)
        prompt_tokens = sum(_approx_token_count(m.content) for m in messages)
        completion_tokens = _approx_token_count(content)
        raw: dict[str, Any] = {
            "provider": self.name,
            "model": model,
            "seed": seed,
            "temperature": temperature,
        }
        return ChatResult(
            content=content,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
            cache_hit=False,
            raw=raw,
        )

    def _next_response(self, messages: list[Message], model: str) -> str:
        if self._queue:
            return self._queue.popleft()
        if self._fixed is not None:
            return self._fixed
        if self._responder is not None:
            return self._responder(messages, model)
        raise LLMResponseError(
            "MockLLMProvider has no configured response; call set_response, "
            "queue_responses, or set_responder before invoking chat()."
        )


__all__ = ["MockCall", "MockLLMProvider"]
