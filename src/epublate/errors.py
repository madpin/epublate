"""Typed exception hierarchy for epublate.

Every recoverable failure should raise (or be converted into) a subclass of
``EpublateError`` so the worker boundary can decide what to retry, what to
record in ``llm_call``, and what to surface in the Inbox.
"""

from __future__ import annotations


class EpublateError(Exception):
    """Base class for all epublate-raised errors."""


class ConfigurationError(EpublateError):
    """User-supplied configuration is missing or invalid."""


class DatabaseError(EpublateError):
    """A persistence-layer operation failed."""


class MigrationError(DatabaseError):
    """An Alembic migration could not be applied."""


class LLMError(EpublateError):
    """Base class for LLM provider failures."""


class LLMTransportError(LLMError):
    """Transport / network failure while talking to an LLM endpoint."""


class LLMResponseError(LLMError):
    """The LLM returned a response that could not be parsed or validated."""


class LLMRateLimitError(LLMResponseError):
    """The LLM endpoint returned HTTP 429 (rate limit / quota exceeded).

    Distinct from :class:`LLMResponseError` so the orchestrator (the
    batch runner, the helper extractor) can recognize a "stop the
    world, the curator needs to act" condition and pause the run
    cleanly — without this signal a depleted free-tier quota on
    OpenRouter would otherwise burn one 429 per segment for the
    remainder of the batch.

    ``retry_after_seconds`` is parsed best-effort from the response
    headers (``Retry-After`` / ``X-RateLimit-Reset``); it's ``None``
    when the endpoint didn't tell us when the quota resets, in which
    case the orchestrator surfaces a generic "switch model or wait"
    message instead of pinning a wall-clock estimate.

    ``provider_message`` echoes the raw ``error.message`` from the
    provider so the curator sees the actionable hint
    (e.g. "Add 6.55 credits to unlock 1000 free model requests
    per day") verbatim in the pause reason.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        provider_message: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.provider_message = provider_message


class FormatError(EpublateError):
    """A format adapter (ePub, future PDF, ...) could not parse or write."""


class GlossaryViolation(EpublateError):
    """A translation violated a ``locked`` glossary entry."""


__all__ = [
    "ConfigurationError",
    "DatabaseError",
    "EpublateError",
    "FormatError",
    "GlossaryViolation",
    "LLMError",
    "LLMRateLimitError",
    "LLMResponseError",
    "LLMTransportError",
    "MigrationError",
]
