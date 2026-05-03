"""Soft-fallback wrapper for chat calls that request JSON mode.

Some OpenAI-compatible endpoints reject ``response_format=json_object``
in narrow conditions:

* **Groq** (proxied via LiteLLM in the wild) runs a grammar validator
  *after* generation and returns a 400 with ``json_validate_failed``
  when the visible content is empty — the typical failure for a
  reasoning helper (``gpt-oss-20b``, ``deepseek-r1``, …) that consumes
  its visible-channel token budget on reasoning tokens before emitting
  any JSON.
* Older self-hosted llama.cpp / Ollama builds return a 400 with
  ``response_format`` mentioned somewhere in the body.

Hard-failing the entire request makes the helper-LLM extractor brittle
on these endpoints. :func:`chat_with_json_fallback` retries the call
once *without* ``response_format`` so the prompt's "respond with JSON
only" instruction is the only constraint left — ugly, but tolerant.
Endpoints that work cleanly (OpenAI proper, OpenRouter, vLLM, the
non-Groq LiteLLM backends, recent Ollama) never hit the fallback.

Cost: one extra round-trip per call on incompatible endpoints. The
pre-pass / intake helpers run at concurrency 1 and produce small
responses, so the doubled cost is bounded; curators who want to skip
JSON mode entirely on those endpoints can pass
``ExtractOptions(response_format=ResponseFormat(type="text"))`` at the
call site.
"""

from __future__ import annotations

import logging

from epublate.errors import LLMRateLimitError, LLMResponseError
from epublate.llm.base import ChatResult, LLMProvider, Message, ResponseFormat

_logger = logging.getLogger(__name__)

# Substrings (lowercased) that signal "the endpoint refused our
# response_format hint, not the prompt itself". Conservative on
# purpose: a true 400 about the prompt content (e.g. "messages too
# long") should still propagate so the curator sees it. We match
# against the raw exception message, which carries the upstream
# provider error verbatim per ``OpenAICompatProvider``.
_RESPONSE_FORMAT_ERROR_PATTERNS: tuple[str, ...] = (
    "json_validate_failed",
    "response_format",
    "json mode",
    "structured output",
    "response format",
)

_LOG_TRUNCATION_LIMIT = 240


def chat_with_json_fallback(
    provider: LLMProvider,
    messages: list[Message],
    *,
    model: str,
    response_format: ResponseFormat | None,
    temperature: float | None = None,
    seed: int | None = None,
) -> ChatResult:
    """Call ``provider.chat`` with a one-shot fallback for JSON-mode rejections.

    Returns the :class:`ChatResult` of whichever call succeeded. On a
    non-``response_format`` error (transport failure, HTTP 401/403, a
    400 unrelated to JSON mode, …) the original exception propagates so
    the caller's audit trail records the real cause.
    """

    try:
        return provider.chat(
            messages,
            model=model,
            response_format=response_format,
            temperature=temperature,
            seed=seed,
        )
    except LLMRateLimitError:
        # Never silently retry a 429 — the orchestrator (batch / pre-pass)
        # surfaces it as a typed pause-the-world signal so curators don't
        # see one rate-limit error per segment for the rest of the run.
        raise
    except LLMResponseError as exc:
        if response_format is None or not _is_json_mode_error(exc):
            raise
        _logger.warning(
            "JSON-mode rejected by helper endpoint; retrying once without "
            "response_format. First-call error: %s",
            _truncate(str(exc), _LOG_TRUNCATION_LIMIT),
        )
        return provider.chat(
            messages,
            model=model,
            response_format=None,
            temperature=temperature,
            seed=seed,
        )


def _is_json_mode_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(pat in text for pat in _RESPONSE_FORMAT_ERROR_PATTERNS)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "\u2026"


__all__ = ["chat_with_json_fallback"]
