"""DB-backed LLM call cache (PRD F-LLM-6 / db-and-persistence rule).

The cache lives in the existing ``llm_call`` table; the ``cache_key``
column added in migration ``0002`` lets us look up a previous identical
call without a separate KV store.

Cache key (PRD §6.1 LLM rule):

    blake2b(model : system_hash : user_hash : glossary_hash)

* ``system_hash`` and ``user_hash`` are SHA-256 of the prompts as they
  would be sent to the model.
* ``glossary_hash`` is a deterministic hash of the glossary state used to
  build the system prompt. M2 ships an empty-glossary sentinel; M3
  upgrades to a real digest when the lore bible lands.
* ``model`` is included so swapping translator models invalidates the
  cache automatically (a 4o-mini cached pair must not satisfy a 4o
  request).

Cache hits set ``llm_call.cache_hit = 1`` and ``cost_usd = 0`` per the
LLM-integration rule.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence

from epublate.llm.base import Message

EMPTY_GLOSSARY_HASH = "0" * 32


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_messages(messages: Sequence[Message]) -> tuple[str, str]:
    """Return ``(system_hash, user_hash)`` for ``messages``.

    The system part folds *all* system messages together (rare to have
    more than one in M2 but harmless); the user part folds everything
    else, preserving role + order so role rearrangement invalidates the
    cache.
    """

    system_parts: list[str] = []
    user_parts: list[str] = []
    for msg in messages:
        if msg.role == "system":
            system_parts.append(msg.content)
        else:
            user_parts.append(f"{msg.role}\u0000{msg.content}")
    system_hash = _sha256("\u0001".join(system_parts))
    user_hash = _sha256("\u0001".join(user_parts))
    return system_hash, user_hash


def cache_key(
    *,
    model: str,
    system_hash: str,
    user_hash: str,
    glossary_hash: str = EMPTY_GLOSSARY_HASH,
) -> str:
    """Deterministic cache key for an LLM call (PRD F-LLM-6)."""

    digest = hashlib.blake2b(
        ":".join((model, system_hash, user_hash, glossary_hash)).encode("utf-8"),
        digest_size=16,
    )
    return digest.hexdigest()


def cache_key_for_messages(
    *,
    model: str,
    messages: Sequence[Message],
    glossary_hash: str = EMPTY_GLOSSARY_HASH,
) -> str:
    """Convenience wrapper: hash ``messages`` then build the cache key."""

    system_hash, user_hash = hash_messages(messages)
    return cache_key(
        model=model,
        system_hash=system_hash,
        user_hash=user_hash,
        glossary_hash=glossary_hash,
    )


__all__ = [
    "EMPTY_GLOSSARY_HASH",
    "cache_key",
    "cache_key_for_messages",
    "hash_messages",
]
