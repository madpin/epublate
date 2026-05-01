"""Tests for the structural placeholder validator."""

from __future__ import annotations

import pytest

from epublate.core.validators import validate_segment_placeholders
from epublate.errors import FormatError
from epublate.formats.base import InlineToken, Segment


def _seg(source: str, *, target: str | None, skeleton: list[InlineToken]) -> Segment:
    return Segment(
        id="seg-1",
        chapter_id="chap-1",
        idx=0,
        source_text=source,
        source_hash="0" * 64,
        target_text=target,
        inline_skeleton=skeleton,
        host_path="/x",
    )


def test_pair_token_valid_round_trip() -> None:
    skeleton = [InlineToken(tag="em", kind="pair")]
    seg = _seg(
        "The [[T0]]old[[/T0]] man.",
        target="O [[T0]]velho[[/T0]] homem.",
        skeleton=skeleton,
    )
    validate_segment_placeholders(seg)


def test_void_token_valid() -> None:
    skeleton = [InlineToken(tag="br", kind="void")]
    seg = _seg(
        "Line one[[T0]]Line two",
        target="Linha um[[T0]]Linha dois",
        skeleton=skeleton,
    )
    validate_segment_placeholders(seg)


def test_missing_closer_raises() -> None:
    skeleton = [InlineToken(tag="em", kind="pair")]
    seg = _seg(
        "[[T0]]old[[/T0]] man",
        target="velho homem",
        skeleton=skeleton,
    )
    with pytest.raises(FormatError):
        validate_segment_placeholders(seg)


def test_extra_placeholder_raises() -> None:
    skeleton = [InlineToken(tag="em", kind="pair")]
    seg = _seg(
        "[[T0]]old[[/T0]]",
        target="[[T0]]velho[[/T0]] [[T0]]extra[[/T0]]",
        skeleton=skeleton,
    )
    with pytest.raises(FormatError):
        validate_segment_placeholders(seg)


def test_void_with_closer_raises() -> None:
    skeleton = [InlineToken(tag="br", kind="void")]
    seg = _seg(
        "[[T0]]",
        target="[[T0]][[/T0]]",
        skeleton=skeleton,
    )
    with pytest.raises(FormatError):
        validate_segment_placeholders(seg)


def test_unknown_index_raises() -> None:
    skeleton = [InlineToken(tag="em", kind="pair")]
    seg = _seg(
        "[[T0]]x[[/T0]]",
        target="[[T1]]oops[[/T1]]",
        skeleton=skeleton,
    )
    with pytest.raises(FormatError):
        validate_segment_placeholders(seg)
