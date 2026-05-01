"""OpenAI-compatible LLM provider (PRD §6.6 / F-LLM-1..7).

This is the only network-facing LLM client v1 ships. It targets the
``/v1/chat/completions`` schema and works against any provider that speaks
it: OpenAI itself, Azure OpenAI, OpenRouter, Together, Ollama, vLLM,
llama.cpp, etc. Provider-specific code paths are explicitly forbidden by
the LLM-integration rule.

Design notes:

* The SDK is configured with ``max_retries=0`` because we own the retry
  loop here so we can apply jittered exponential backoff and surface
  failures via the typed ``epublate.errors`` hierarchy.
* Token usage is preferred from the API response; if the endpoint omits
  ``usage`` (Ollama / older llama.cpp builds do this) we fall back to
  :func:`epublate.llm.tokens.count_tokens` so cost tracking still produces
  a non-zero number.
* No API keys ever land in logs: the request payload echoed back to
  callers is constructed from our own ``Message`` objects, not the SDK's.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import openai
from openai import OpenAI
from openai.types.chat import ChatCompletion

from epublate.errors import LLMResponseError, LLMTransportError
from epublate.llm.base import ChatResult, Message, ResponseFormat
from epublate.llm.tokens import count_tokens

_logger = logging.getLogger(__name__)

# ``ChatCompletion`` rejects unknown ``response_format`` payload keys; the
# Chat-Completions API accepts a small, documented vocabulary we validate
# against here. Unknown values surface as ``LLMResponseError`` rather than
# silently being dropped on the wire.
_VALID_RESPONSE_FORMAT_TYPES = frozenset({"text", "json_object", "json_schema"})

_DEFAULT_RETRYABLE_STATUSES: frozenset[int] = frozenset(
    {408, 409, 429, 500, 502, 503, 504}
)

Sleeper = Callable[[float], None]


@dataclass(slots=True)
class RetryPolicy:
    """Exponential-backoff parameters with full jitter."""

    max_retries: int = 4
    initial: float = 0.5
    maximum: float = 8.0
    multiplier: float = 2.0
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        """Return the delay (seconds) to wait *before* attempt ``attempt``."""

        # ``attempt`` is the next attempt index (0 = first call). Backoff
        # only applies on retries (attempt >= 1).
        if attempt <= 0:
            return 0.0
        base = min(self.maximum, self.initial * (self.multiplier ** (attempt - 1)))
        if not self.jitter:
            return base
        # Full jitter (AWS architecture blog) keeps independent clients from
        # synchronizing their retries against the same provider.
        return random.uniform(0.0, base)


@dataclass(slots=True)
class OpenAICompatProvider:
    """OpenAI-compatible chat-completions provider (PRD F-LLM-1)."""

    name: str = "openai_compat"
    base_url: str | None = None
    api_key: str = ""
    default_model: str | None = None
    organization: str | None = None
    timeout: float = 60.0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    http_client: httpx.Client | None = None
    sleep: Sleeper = time.sleep

    _client: OpenAI = field(init=False)

    def __post_init__(self) -> None:
        # Default for local / OSS endpoints that ignore auth: keep an empty
        # placeholder so the SDK doesn't read ``OPENAI_API_KEY`` from the
        # environment by surprise (NFR-3 privacy boundary).
        api_key = self.api_key or "epublate-no-key"
        kwargs: dict[str, Any] = {
            "api_key": api_key,
            "max_retries": 0,
            "timeout": self.timeout,
        }
        if self.base_url:
            kwargs["base_url"] = self.base_url
        if self.organization:
            kwargs["organization"] = self.organization
        if self.http_client is not None:
            kwargs["http_client"] = self.http_client
        self._client = OpenAI(**kwargs)

    # -------- LLMProvider surface --------

    def chat(
        self,
        messages: list[Message],
        *,
        model: str,
        response_format: ResponseFormat | None = None,
        temperature: float | None = None,
        seed: int | None = None,
    ) -> ChatResult:
        """Issue a chat-completion with retry/backoff and structured output."""

        if not messages:
            raise LLMResponseError("messages must not be empty")

        chosen_model = model or self.default_model
        if not chosen_model:
            raise LLMResponseError(
                "no model: pass `model=` to chat() or set "
                "OpenAICompatProvider.default_model"
            )

        sdk_messages = [{"role": m.role, "content": m.content} for m in messages]
        sdk_kwargs: dict[str, Any] = {
            "model": chosen_model,
            "messages": sdk_messages,
        }
        if temperature is not None:
            sdk_kwargs["temperature"] = temperature
        if seed is not None:
            sdk_kwargs["seed"] = seed
        if response_format is not None:
            sdk_kwargs["response_format"] = _serialize_response_format(response_format)

        last_exc: Exception | None = None
        attempts = self.retry_policy.max_retries + 1
        for attempt in range(attempts):
            delay = self.retry_policy.delay_for(attempt)
            if delay > 0:
                self.sleep(delay)
            try:
                completion: ChatCompletion = self._client.chat.completions.create(
                    **sdk_kwargs,
                )
            except openai.APIStatusError as exc:
                if (
                    exc.status_code in _DEFAULT_RETRYABLE_STATUSES
                    and attempt < attempts - 1
                ):
                    last_exc = exc
                    _logger.debug(
                        "retryable openai status %s on attempt %d/%d",
                        exc.status_code,
                        attempt + 1,
                        attempts,
                    )
                    continue
                raise LLMResponseError(
                    f"OpenAI API status {exc.status_code}: {exc.message}"
                ) from exc
            except (openai.APIConnectionError, openai.APITimeoutError) as exc:
                if attempt < attempts - 1:
                    last_exc = exc
                    _logger.debug(
                        "retryable transport error on attempt %d/%d: %s",
                        attempt + 1,
                        attempts,
                        type(exc).__name__,
                    )
                    continue
                raise LLMTransportError(str(exc)) from exc
            except httpx.TransportError as exc:
                if attempt < attempts - 1:
                    last_exc = exc
                    _logger.debug(
                        "retryable httpx transport error on attempt %d/%d",
                        attempt + 1,
                        attempts,
                    )
                    continue
                raise LLMTransportError(str(exc)) from exc

            return _to_chat_result(completion, model=chosen_model, sent=messages)

        # Exhausted retries on a retryable error; surface the last one.
        if isinstance(last_exc, openai.APIStatusError):
            raise LLMResponseError(
                f"OpenAI API status {last_exc.status_code} after {attempts} attempts"
            ) from last_exc
        if last_exc is not None:
            raise LLMTransportError(
                f"transport error after {attempts} attempts: {last_exc}"
            ) from last_exc
        # Should be unreachable: if no exception occurred we returned above.
        raise LLMTransportError("retry loop exited without a result")


def _serialize_response_format(rf: ResponseFormat) -> dict[str, Any]:
    if rf.type not in _VALID_RESPONSE_FORMAT_TYPES:
        raise LLMResponseError(f"unknown response_format type: {rf.type!r}")
    payload: dict[str, Any] = {"type": rf.type}
    if rf.type == "json_schema":
        if rf.schema_ is None:
            raise LLMResponseError(
                "response_format.type='json_schema' requires a schema"
            )
        payload["json_schema"] = rf.schema_
    return payload


def _to_chat_result(
    completion: ChatCompletion, *, model: str, sent: list[Message]
) -> ChatResult:
    if not completion.choices:
        raise LLMResponseError("chat-completion returned no choices")
    content = completion.choices[0].message.content or ""

    usage = completion.usage
    if usage is not None:
        prompt_tokens = int(usage.prompt_tokens or 0)
        completion_tokens = int(usage.completion_tokens or 0)
    else:
        prompt_tokens = sum(count_tokens(m.content, model=model) for m in sent)
        completion_tokens = count_tokens(content, model=model)

    raw = completion.model_dump(mode="json")

    return ChatResult(
        content=content,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        model=completion.model or model,
        cache_hit=False,
        raw=raw,
    )


__all__ = [
    "OpenAICompatProvider",
    "RetryPolicy",
]
