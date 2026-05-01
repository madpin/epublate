"""Token accounting (PRD F-LLM-4).

A thin façade around ``tiktoken.encoding_for_model()`` so the rest of the
codebase doesn't import ``tiktoken`` directly. When a model is unknown to
``tiktoken`` we fall back to the same chars/4 heuristic the M1 segmenter uses
— monotonic, deterministic, and good enough for budgeting.

The module avoids loading any tokenizer at import time; encoders are cached
on first use so tests that exercise only the fallback never pay the cost of
downloading a vocabulary.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

# Models we explicitly know map onto a ``cl100k_base``-class encoder; this
# spares ``tiktoken`` a network/disk lookup the first time we count for an
# OpenAI-compatible endpoint that uses an unknown public model name.
_KNOWN_MODELS: frozenset[str] = frozenset(
    {
        "gpt-3.5-turbo",
        "gpt-4",
        "gpt-4-turbo",
        "gpt-4o",
        "gpt-5-mini",
    }
)


def _heuristic(text: str) -> int:
    """Cheap chars/4 estimate; bounded below by 1 for any non-empty string."""

    if not text:
        return 0
    return max(1, len(text) // 4)


@lru_cache(maxsize=16)
def _encoder_for(model: str) -> Any | None:
    """Return a ``tiktoken`` encoder for ``model``, or ``None`` if unknown.

    ``tiktoken`` is imported lazily because it pulls in ``regex`` and a
    vocabulary file the first time an encoder is constructed.
    """

    try:
        import tiktoken
    except ImportError:
        return None

    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        if model in _KNOWN_MODELS:
            try:
                return tiktoken.get_encoding("cl100k_base")
            except (KeyError, ValueError):
                return None
        return None
    except (ValueError, RuntimeError):
        return None


def count_tokens(text: str, *, model: str | None = None) -> int:
    """Count tokens in ``text`` for ``model``.

    Falls back to the chars/4 heuristic when ``model`` is ``None`` or unknown
    to ``tiktoken``. Always returns ``0`` for the empty string.
    """

    if not text:
        return 0
    if model is None:
        return _heuristic(text)
    encoder = _encoder_for(model)
    if encoder is None:
        return _heuristic(text)
    return len(encoder.encode(text))


__all__ = ["count_tokens"]
