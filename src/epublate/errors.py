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
    "LLMResponseError",
    "LLMTransportError",
    "MigrationError",
]
