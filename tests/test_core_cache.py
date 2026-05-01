"""Tests for the deterministic LLM cache key (PRD F-LLM-6)."""

from __future__ import annotations

from epublate.core.cache import (
    EMPTY_GLOSSARY_HASH,
    cache_key,
    cache_key_for_messages,
    hash_messages,
)
from epublate.llm.base import Message


def _msg(role: str, content: str) -> Message:
    return Message(role=role, content=content)  # type: ignore[arg-type]


def test_key_is_deterministic() -> None:
    msgs = [_msg("system", "S"), _msg("user", "U")]
    a = cache_key_for_messages(model="gpt-5-mini", messages=msgs)
    b = cache_key_for_messages(model="gpt-5-mini", messages=msgs)
    assert a == b


def test_key_varies_with_model() -> None:
    msgs = [_msg("system", "S"), _msg("user", "U")]
    a = cache_key_for_messages(model="gpt-5-mini", messages=msgs)
    b = cache_key_for_messages(model="gpt-4o", messages=msgs)
    assert a != b


def test_key_varies_with_user_prompt() -> None:
    base = [_msg("system", "S")]
    a = cache_key_for_messages(model="m", messages=[*base, _msg("user", "U1")])
    b = cache_key_for_messages(model="m", messages=[*base, _msg("user", "U2")])
    assert a != b


def test_key_varies_with_system_prompt() -> None:
    msgs1 = [_msg("system", "S1"), _msg("user", "U")]
    msgs2 = [_msg("system", "S2"), _msg("user", "U")]
    a = cache_key_for_messages(model="m", messages=msgs1)
    b = cache_key_for_messages(model="m", messages=msgs2)
    assert a != b


def test_key_varies_with_glossary_hash() -> None:
    msgs = [_msg("system", "S"), _msg("user", "U")]
    a = cache_key_for_messages(model="m", messages=msgs)
    b = cache_key_for_messages(model="m", messages=msgs, glossary_hash="deadbeef" * 4)
    assert a != b


def test_hash_messages_returns_hex_strings() -> None:
    msgs = [_msg("system", "S"), _msg("user", "U")]
    sys_h, user_h = hash_messages(msgs)
    assert len(sys_h) == 64
    assert len(user_h) == 64
    int(sys_h, 16)  # must be valid hex
    int(user_h, 16)


def test_empty_glossary_sentinel_is_constant() -> None:
    assert EMPTY_GLOSSARY_HASH == "0" * 32


def test_role_order_changes_user_hash() -> None:
    a = hash_messages([_msg("user", "first"), _msg("assistant", "second")])
    b = hash_messages([_msg("assistant", "second"), _msg("user", "first")])
    assert a != b


def test_low_level_cache_key_function() -> None:
    key = cache_key(
        model="gpt-5-mini",
        system_hash="a" * 64,
        user_hash="b" * 64,
        glossary_hash="0" * 32,
    )
    # blake2b digest_size=16 -> 32 hex characters.
    assert len(key) == 32
    int(key, 16)
