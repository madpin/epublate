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

from epublate.errors import LLMRateLimitError, LLMResponseError, LLMTransportError
from epublate.llm.base import ChatResult, Message, ResponseFormat
from epublate.llm.tokens import count_tokens

_logger = logging.getLogger(__name__)

# ``ChatCompletion`` rejects unknown ``response_format`` payload keys; the
# Chat-Completions API accepts a small, documented vocabulary we validate
# against here. Unknown values surface as ``LLMResponseError`` rather than
# silently being dropped on the wire.
_VALID_RESPONSE_FORMAT_TYPES = frozenset({"text", "json_object", "json_schema"})

# 429 is excluded here on purpose — we handle it separately so a depleted
# daily quota lifts out as a typed :class:`LLMRateLimitError` instead of
# burning the per-call retry budget on a quota that won't reset for hours.
_DEFAULT_RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 409, 500, 502, 503, 504})

# Honor the provider's Retry-After hint up to this cap. Above it we treat
# the wait as unbounded (depleted free-tier daily quota, monthly cap, etc.)
# and surface the rate-limit error so the orchestrator pauses cleanly. A
# TUI batch can plausibly wait a couple of minutes for a burst limit to
# clear; sleeping for hours on a worker thread would freeze the UI and
# look like a hang.
_RATE_LIMIT_SHORT_WAIT_CAP_SECONDS = 120.0

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


_VALID_REASONING_EFFORTS: frozenset[str] = frozenset(
    {"minimal", "low", "medium", "high"}
)


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
    reasoning_effort: str | None = None
    """Standard OpenAI ``reasoning_effort`` field for reasoning models.

    One of ``minimal | low | medium | high`` or ``None`` to omit the
    field entirely (the right default for non-reasoning models, which
    would otherwise reject the unknown parameter on stricter
    endpoints). Reasoning models like ``gpt-5-*``, ``gpt-oss-*``,
    ``o1-*``, ``o3-*``, and the Nemotron ``-reasoning`` slugs spend
    most of their wall-clock time on hidden chain-of-thought; lowering
    the effort cuts latency dramatically (typically 3-10x for ``low``)
    at a small quality cost.

    The cache key (PRD §6.4) does not include this knob. That's a
    pragmatic choice: a curator who flips ``low`` ↔ ``high`` mid-batch
    is rare, and re-using cached high-effort responses for a low-effort
    re-run is strictly an improvement (better answer, free).
    """

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

        if (
            self.reasoning_effort is not None
            and self.reasoning_effort not in _VALID_REASONING_EFFORTS
        ):
            raise LLMResponseError(
                f"reasoning_effort must be one of "
                f"{sorted(_VALID_REASONING_EFFORTS)} or None, "
                f"got {self.reasoning_effort!r}"
            )

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
        if self.reasoning_effort is not None:
            sdk_kwargs["reasoning_effort"] = self.reasoning_effort

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
            except openai.RateLimitError as exc:
                retry_after = _parse_retry_after(exc)
                # When the headers tell us the wait is longer than the
                # per-call retry budget can absorb (depleted daily /
                # monthly quotas — the OpenRouter free tier
                # "free-models-per-day" reset lands at UTC midnight,
                # i.e. up to 24h away) we lift out as a typed error so
                # the orchestrator pauses the batch instead of sleeping
                # a worker thread for hours.
                if (
                    retry_after is not None
                    and retry_after > _RATE_LIMIT_SHORT_WAIT_CAP_SECONDS
                ):
                    raise LLMRateLimitError(
                        f"OpenAI API status {exc.status_code}: {exc.message}",
                        retry_after_seconds=retry_after,
                        provider_message=str(exc.message),
                    ) from exc
                if attempt < attempts - 1:
                    last_exc = exc
                    # Honor the server's hint when present (RFC 6585);
                    # if no hint, the next iteration's exponential backoff
                    # at the loop head still applies.
                    if retry_after is not None and retry_after > 0:
                        _logger.warning(
                            "rate limit hit; sleeping %.1fs per Retry-After "
                            "before attempt %d/%d",
                            retry_after,
                            attempt + 2,
                            attempts,
                        )
                        self.sleep(retry_after)
                    else:
                        _logger.debug(
                            "rate limit hit; no Retry-After header. "
                            "Backing off per policy. Attempt %d/%d.",
                            attempt + 1,
                            attempts,
                        )
                    continue
                # Retries exhausted: still surface as a typed rate-limit
                # error so the orchestrator can pause cleanly even when
                # the endpoint didn't supply a header hint.
                raise LLMRateLimitError(
                    f"OpenAI API status {exc.status_code}: {exc.message}",
                    retry_after_seconds=retry_after,
                    provider_message=str(exc.message),
                ) from exc
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


def _parse_retry_after(exc: openai.APIStatusError) -> float | None:
    """Best-effort extraction of "wait this long before retrying" from a 429.

    The HTTP standard is ``Retry-After`` in seconds (RFC 6585), which OpenAI
    itself sets on burst rate limits. OpenRouter follows the
    ``X-RateLimit-Reset`` convention with a *millisecond* epoch timestamp
    so consumers can pin the exact reset moment (``X-RateLimit-Reset:
    1777852800000`` → "free-models-per-day quota resets at UTC midnight").

    Returns the implied wait time in seconds, or ``None`` if no usable hint
    is present. Negative values (clock skew) are clamped to zero.
    """

    response = getattr(exc, "response", None)
    if response is None:
        return None
    headers = getattr(response, "headers", None)
    if headers is None:
        return None

    def _get(key: str) -> str | None:
        # ``httpx.Headers`` is case-insensitive; for plain dicts in test
        # doubles we try both casings rather than depending on a specific
        # mapping shape.
        try:
            value = headers.get(key)
        except (AttributeError, TypeError):
            return None
        if value is None and isinstance(headers, dict):
            value = headers.get(key.lower()) or headers.get(key.title())
        return None if value is None else str(value)

    retry_after = _get("Retry-After")
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            # Servers may send an HTTP-date here; we don't bother parsing.
            pass

    reset_ms = _get("X-RateLimit-Reset")
    if reset_ms:
        try:
            reset_epoch_s = float(reset_ms) / 1000.0
        except ValueError:
            return None
        delta = reset_epoch_s - time.time()
        return max(0.0, delta)

    return None


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
