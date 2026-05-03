"""Tests for the OpenAI-compatible provider (PRD F-LLM-1..7).

Exercises happy path, retry/backoff on 429/5xx, non-retryable errors, and
that ``temperature``/``seed``/``response_format`` are forwarded correctly.
All requests are intercepted with ``httpx.MockTransport`` so no socket is
opened (NFR-7).
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx
import pytest

from epublate.errors import LLMRateLimitError, LLMResponseError, LLMTransportError
from epublate.llm import LLMProvider
from epublate.llm.base import Message, ResponseFormat
from epublate.llm.openai_compat import OpenAICompatProvider, RetryPolicy


def _msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)  # type: ignore[arg-type]


def _completion_body(content: str, *, model: str = "gpt-mock") -> dict[str, object]:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 7,
            "completion_tokens": 3,
            "total_tokens": 10,
        },
    }


@pytest.fixture
def captured_requests() -> list[httpx.Request]:
    return []


def _make_provider(
    handler: httpx.MockTransport,
    *,
    captured: list[httpx.Request],
    retries: int = 0,
    reasoning_effort: str | None = None,
) -> OpenAICompatProvider:
    """Build a provider whose http client routes through ``handler``."""

    def _instrument(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return handler.handle_request(request)

    transport = httpx.MockTransport(_instrument)
    client = httpx.Client(transport=transport, base_url="http://mocked")
    return OpenAICompatProvider(
        base_url="http://mocked/v1",
        api_key="test-key",
        default_model="gpt-mock",
        retry_policy=RetryPolicy(
            max_retries=retries, initial=0.0, maximum=0.0, jitter=False
        ),
        http_client=client,
        sleep=lambda _s: None,
        reasoning_effort=reasoning_effort,
    )


def test_provider_implements_protocol() -> None:
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, json=_completion_body("ok"))
    )
    captured: list[httpx.Request] = []
    provider = _make_provider(transport, captured=captured)
    assert isinstance(provider, LLMProvider)


def test_happy_path_returns_chat_result(
    captured_requests: list[httpx.Request],
) -> None:
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, json=_completion_body("hola"))
    )
    provider = _make_provider(transport, captured=captured_requests)
    result = provider.chat([_msg("user", "hello")], model="gpt-mock")
    assert result.content == "hola"
    assert result.prompt_tokens == 7
    assert result.completion_tokens == 3
    assert result.model == "gpt-mock"
    assert result.cache_hit is False
    assert len(captured_requests) == 1


def test_request_carries_seed_temperature_and_response_format(
    captured_requests: list[httpx.Request],
) -> None:
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, json=_completion_body("ok"))
    )
    provider = _make_provider(transport, captured=captured_requests)
    provider.chat(
        [_msg("system", "be brief"), _msg("user", "yo")],
        model="gpt-mock",
        temperature=0.0,
        seed=7,
        response_format=ResponseFormat(type="json_object"),
    )
    body = json.loads(captured_requests[0].content.decode("utf-8"))
    assert body["model"] == "gpt-mock"
    assert body["temperature"] == 0.0
    assert body["seed"] == 7
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"][-1] == {"role": "user", "content": "yo"}


def test_429_retried_then_succeeds(
    captured_requests: list[httpx.Request],
) -> None:
    responses: Iterator[httpx.Response] = iter(
        [
            httpx.Response(429, json={"error": {"message": "slow down"}}),
            httpx.Response(200, json=_completion_body("ok")),
        ]
    )
    transport = httpx.MockTransport(lambda _r: next(responses))
    provider = _make_provider(transport, captured=captured_requests, retries=2)
    result = provider.chat([_msg("user", "x")], model="gpt-mock")
    assert result.content == "ok"
    assert len(captured_requests) == 2


def test_500_retried_then_succeeds(
    captured_requests: list[httpx.Request],
) -> None:
    responses: Iterator[httpx.Response] = iter(
        [
            httpx.Response(500, json={"error": {"message": "boom"}}),
            httpx.Response(200, json=_completion_body("recovered")),
        ]
    )
    transport = httpx.MockTransport(lambda _r: next(responses))
    provider = _make_provider(transport, captured=captured_requests, retries=2)
    result = provider.chat([_msg("user", "x")], model="gpt-mock")
    assert result.content == "recovered"
    assert len(captured_requests) == 2


def test_400_is_not_retried_and_raises(
    captured_requests: list[httpx.Request],
) -> None:
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(400, json={"error": {"message": "bad"}})
    )
    provider = _make_provider(transport, captured=captured_requests, retries=3)
    with pytest.raises(LLMResponseError):
        provider.chat([_msg("user", "x")], model="gpt-mock")
    assert len(captured_requests) == 1


def test_retries_exhausted_surfaces_typed_error(
    captured_requests: list[httpx.Request],
) -> None:
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(503, json={"error": {"message": "down"}})
    )
    provider = _make_provider(transport, captured=captured_requests, retries=2)
    with pytest.raises(LLMResponseError):
        provider.chat([_msg("user", "x")], model="gpt-mock")
    # Initial attempt + 2 retries = 3.
    assert len(captured_requests) == 3


def test_transport_error_retried_then_raises_typed_error(
    captured_requests: list[httpx.Request],
) -> None:
    def _boom(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    transport = httpx.MockTransport(_boom)
    provider = _make_provider(transport, captured=captured_requests, retries=1)
    with pytest.raises(LLMTransportError):
        provider.chat([_msg("user", "x")], model="gpt-mock")
    # Initial + 1 retry = 2 connect attempts.
    assert len(captured_requests) == 2


def test_retry_policy_delay_grows() -> None:
    policy = RetryPolicy(max_retries=4, initial=0.5, maximum=8.0, jitter=False)
    assert policy.delay_for(0) == 0.0
    assert policy.delay_for(1) == 0.5
    assert policy.delay_for(2) == 1.0
    assert policy.delay_for(3) == 2.0
    assert policy.delay_for(10) == 8.0


def test_missing_usage_falls_back_to_local_token_count(
    captured_requests: list[httpx.Request],
) -> None:
    body = _completion_body("xyz")
    body.pop("usage")
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json=body))
    provider = _make_provider(transport, captured=captured_requests)
    result = provider.chat([_msg("user", "abcdefgh")], model="gpt-mock")
    # Heuristic chars/4 against "abcdefgh" is 2; against "xyz" is 1.
    assert result.prompt_tokens >= 1
    assert result.completion_tokens >= 1


def test_empty_messages_rejected() -> None:
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, json=_completion_body("never"))
    )
    captured: list[httpx.Request] = []
    provider = _make_provider(transport, captured=captured)
    with pytest.raises(LLMResponseError):
        provider.chat([], model="gpt-mock")
    assert captured == []


def test_reasoning_effort_attached_when_set(
    captured_requests: list[httpx.Request],
) -> None:
    """Setting ``reasoning_effort`` on the provider attaches the same
    field to every chat-completions request body. This is the standard
    OpenAI knob (``minimal | low | medium | high``) for reasoning
    models — gpt-5/o1/o3 honor it directly, OpenRouter forwards it to
    upstream, and non-reasoning models on most endpoints just ignore
    it. Lowering effort is the cheapest single fix for "reasoning
    helper is too slow" complaints (PRD §4.2 phase 3 / M5)."""

    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, json=_completion_body("ok"))
    )
    provider = _make_provider(
        transport, captured=captured_requests, reasoning_effort="low"
    )
    provider.chat([_msg("user", "hi")], model="gpt-mock")
    body = json.loads(captured_requests[0].content.decode("utf-8"))
    assert body["reasoning_effort"] == "low"


def test_reasoning_effort_omitted_when_unset(
    captured_requests: list[httpx.Request],
) -> None:
    """When ``reasoning_effort`` is ``None`` (the default), the field
    is *omitted* from the request body — non-reasoning models on
    stricter endpoints would otherwise reject the unknown parameter."""

    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, json=_completion_body("ok"))
    )
    provider = _make_provider(transport, captured=captured_requests)
    provider.chat([_msg("user", "hi")], model="gpt-mock")
    body = json.loads(captured_requests[0].content.decode("utf-8"))
    assert "reasoning_effort" not in body


@pytest.mark.parametrize("value", ["minimal", "low", "medium", "high"])
def test_reasoning_effort_accepts_valid_values(value: str) -> None:
    """The four standard OpenAI reasoning-effort settings are all
    allowed; any other string raises at construction time so a typo
    in ``$EPUBLATE_LLM_REASONING_EFFORT`` fails fast instead of
    surfacing as a 400 from the endpoint mid-batch."""

    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, json=_completion_body("ok"))
    )
    captured: list[httpx.Request] = []
    provider = _make_provider(transport, captured=captured, reasoning_effort=value)
    assert provider.reasoning_effort == value


def test_reasoning_effort_invalid_value_raises() -> None:
    """A typo in the env var should fail at provider build time, not
    after the first chat() call hits the wire."""

    with pytest.raises(LLMResponseError, match="reasoning_effort"):
        OpenAICompatProvider(
            base_url="http://mocked/v1",
            api_key="test-key",
            default_model="gpt-mock",
            reasoning_effort="aggressive",
        )


def test_unknown_response_format_type_rejected() -> None:
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(200, json=_completion_body("never"))
    )
    captured: list[httpx.Request] = []
    provider = _make_provider(transport, captured=captured)
    with pytest.raises(LLMResponseError):
        provider.chat(
            [_msg("user", "x")],
            model="gpt-mock",
            response_format=ResponseFormat.model_construct(  # bypass validation
                type="bogus"
            ),
        )


def test_429_with_long_x_ratelimit_reset_raises_rate_limit_error(
    captured_requests: list[httpx.Request],
) -> None:
    """OpenRouter free-tier "free-models-per-day" quota lifts out cleanly.

    The depleted-quota response carries ``X-RateLimit-Reset`` set to the
    millisecond epoch when the quota refills (typically UTC midnight).
    When the implied wait is longer than the per-call retry budget can
    absorb (``> 120s``) we must raise ``LLMRateLimitError`` immediately
    so the orchestrator pauses cleanly instead of burning the rest of
    the queue on the same 429.
    """

    import time

    # Reset 1 hour from now, well above the 120s short-wait cap.
    reset_ms = int((time.time() + 3600) * 1000)
    transport = httpx.MockTransport(
        lambda _r: httpx.Response(
            429,
            json={
                "error": {
                    "message": (
                        "Rate limit exceeded: free-models-per-day. "
                        "Add 6.55 credits to unlock 1000 free model "
                        "requests per day"
                    )
                }
            },
            headers={
                "X-RateLimit-Limit": "50",
                "X-RateLimit-Remaining": "0",
                "X-RateLimit-Reset": str(reset_ms),
            },
        )
    )
    provider = _make_provider(transport, captured=captured_requests, retries=4)
    with pytest.raises(LLMRateLimitError) as exc_info:
        provider.chat([_msg("user", "x")], model="gpt-mock")
    err = exc_info.value
    assert err.retry_after_seconds is not None
    assert err.retry_after_seconds > 120  # minutes-to-hours
    assert "free-models-per-day" in (err.provider_message or "")
    # The first call surfaced the error — no per-call retries burned.
    assert len(captured_requests) == 1


def test_429_with_short_retry_after_sleeps_and_retries(
    captured_requests: list[httpx.Request],
) -> None:
    """A burst rate limit with ``Retry-After: 5`` sleeps that long and retries.

    The OpenAI canonical 429-with-Retry-After case (server hints "wait
    5s and try again") should not pause the batch; we honor the server
    hint, retry within the per-call budget, and succeed on the second
    response.
    """

    responses: Iterator[httpx.Response] = iter(
        [
            httpx.Response(
                429,
                json={"error": {"message": "slow down"}},
                headers={"Retry-After": "5"},
            ),
            httpx.Response(200, json=_completion_body("ok")),
        ]
    )
    sleeps: list[float] = []

    def _instrument(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return next(responses)

    transport = httpx.MockTransport(_instrument)
    client = httpx.Client(transport=transport, base_url="http://mocked")
    provider = OpenAICompatProvider(
        base_url="http://mocked/v1",
        api_key="test-key",
        default_model="gpt-mock",
        retry_policy=RetryPolicy(max_retries=2, initial=0.0, maximum=0.0, jitter=False),
        http_client=client,
        sleep=sleeps.append,
    )
    result = provider.chat([_msg("user", "x")], model="gpt-mock")
    assert result.content == "ok"
    assert len(captured_requests) == 2
    # The 5s Retry-After hint was passed to ``sleep``.
    assert 5.0 in sleeps


def test_429_without_headers_falls_through_to_existing_retry(
    captured_requests: list[httpx.Request],
) -> None:
    """A header-less 429 keeps the legacy "retry per policy" behavior.

    Without a ``Retry-After`` or ``X-RateLimit-Reset`` we can't tell if
    the wait is one second or one day. The conservative default is to
    retry per the existing exponential backoff so a transient 429 burst
    (no headers) doesn't pause the batch.
    """

    responses: Iterator[httpx.Response] = iter(
        [
            httpx.Response(429, json={"error": {"message": "slow down"}}),
            httpx.Response(200, json=_completion_body("ok")),
        ]
    )
    transport = httpx.MockTransport(lambda _r: next(responses))
    provider = _make_provider(transport, captured=captured_requests, retries=2)
    result = provider.chat([_msg("user", "x")], model="gpt-mock")
    assert result.content == "ok"
    assert len(captured_requests) == 2


def test_429_exhausting_retries_surfaces_rate_limit_error(
    captured_requests: list[httpx.Request],
) -> None:
    """When retries don't recover, the typed rate-limit error wins.

    Before this fix every helper-LLM 429 surfaced as a generic
    :class:`LLMResponseError`; the batch worker treated it as an opaque
    per-segment failure and burned the rest of the queue on the same
    rate limit. Surfacing the typed error lets the orchestrator pause.
    """

    transport = httpx.MockTransport(
        lambda _r: httpx.Response(429, json={"error": {"message": "still busy"}})
    )
    provider = _make_provider(transport, captured=captured_requests, retries=2)
    with pytest.raises(LLMRateLimitError):
        provider.chat([_msg("user", "x")], model="gpt-mock")
    # initial + 2 retries = 3 requests
    assert len(captured_requests) == 3
