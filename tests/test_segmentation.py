"""Property + unit tests for :mod:`epublate.core.segmentation`.

Round-trip identity (``apply_parts_to_host(host, [placeholderize(host)])``
produces the same serialized fragment as the input) is the M1 invariant the
ePub adapter relies on (PRD §4.1, format-handling rule).
"""

from __future__ import annotations

import re

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from lxml import etree

from epublate.core.segmentation import (
    PLACEHOLDER_RE,
    apply_parts_to_host,
    count_tokens,
    placeholderize,
    split_by_sentences,
)
from epublate.errors import FormatError
from epublate.formats.base import InlineToken

XHTML_NS = "http://www.w3.org/1999/xhtml"

INLINE_TAGS = ("em", "strong", "a")
VOID_TAGS = ("br",)
SAFE_TEXT = st.text(
    alphabet=st.characters(
        whitelist_categories=("Ll", "Lu", "Nd", "Zs"),
        whitelist_characters=" .,:;-",
    ),
    min_size=0,
    max_size=12,
)


def _inline_node_strategy(depth: int) -> st.SearchStrategy[str]:
    """Hypothesis strategy emitting an inline-content XHTML fragment.

    Recursion is bounded so the property-test runs stay fast on CI.
    """

    leaf = SAFE_TEXT.map(_xml_escape)
    if depth <= 0:
        return leaf
    void_strat = st.sampled_from([f"<{tag}/>" for tag in VOID_TAGS])
    inner = _inline_node_strategy(depth - 1)
    pair_strat = st.builds(
        lambda tag, content: f"<{tag}>{content}</{tag}>",
        st.sampled_from(INLINE_TAGS),
        inner,
    )
    fragment = st.one_of(leaf, void_strat, pair_strat)
    return st.lists(fragment, min_size=0, max_size=4).map("".join)


def _xml_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _wrap_paragraph(inner: str) -> str:
    return f'<p xmlns="{XHTML_NS}">{inner}</p>'


def _serialize(elem: etree._Element) -> str:
    return etree.tostring(elem, encoding="unicode")


def _parse(xml: str) -> etree._Element:
    return etree.fromstring(xml.encode("utf-8"))


@given(_inline_node_strategy(depth=2))
@settings(max_examples=120, deadline=None)
def test_placeholderize_apply_round_trips(inner: str) -> None:
    original = _parse(_wrap_paragraph(inner))
    text, skeleton = placeholderize(original)
    target = _parse(_wrap_paragraph(""))
    apply_parts_to_host(target, [(text, skeleton)])
    assert _serialize(target) == _serialize(original)


@given(_inline_node_strategy(depth=2))
@settings(max_examples=80, deadline=None)
def test_placeholders_are_balanced(inner: str) -> None:
    elem = _parse(_wrap_paragraph(inner))
    text, skeleton = placeholderize(elem)
    open_counts: dict[int, int] = {}
    close_counts: dict[int, int] = {}
    void_counts: dict[int, int] = {}
    for m in PLACEHOLDER_RE.finditer(text):
        idx = int(m.group(2))
        is_close = m.group(1) == "/"
        token = skeleton[idx]
        if token.kind == "void":
            assert not is_close
            void_counts[idx] = void_counts.get(idx, 0) + 1
        elif is_close:
            close_counts[idx] = close_counts.get(idx, 0) + 1
        else:
            open_counts[idx] = open_counts.get(idx, 0) + 1
    for idx, token in enumerate(skeleton):
        if token.kind == "void":
            assert void_counts.get(idx, 0) == 1
        else:
            assert open_counts.get(idx, 0) == 1
            assert close_counts.get(idx, 0) == 1


def test_count_tokens_is_monotonic() -> None:
    assert count_tokens("a") == 1
    assert count_tokens("a" * 4) == 1
    assert count_tokens("a" * 8) == 2
    assert count_tokens("a" * 8) <= count_tokens("a" * 16)


def test_apply_parts_rejects_unmatched_close() -> None:
    target = _parse(_wrap_paragraph(""))
    with pytest.raises(FormatError):
        apply_parts_to_host(
            target,
            [
                (
                    "lonely [[/T0]] close",
                    [InlineToken(tag="em", kind="pair")],
                )
            ],
        )


def test_apply_parts_rejects_unclosed_pair() -> None:
    target = _parse(_wrap_paragraph(""))
    with pytest.raises(FormatError):
        apply_parts_to_host(
            target,
            [
                (
                    "open [[T0]]without close",
                    [InlineToken(tag="em", kind="pair")],
                )
            ],
        )


def test_split_by_sentences_short_text_is_one_chunk() -> None:
    text = "Hello world. Just two sentences."
    chunks = split_by_sentences(text, [], max_tokens=999)
    assert len(chunks) == 1
    assert chunks[0][0] == text


def test_split_by_sentences_never_breaks_inside_pair() -> None:
    skeleton = [InlineToken(tag="em", kind="pair")]
    text = (
        "First sentence is short. Second sentence opens [[T0]]a long, long, "
        "long, long, long, long, long, long emphasized stretch[[/T0]] of "
        "text. Third sentence is also fine."
    )
    chunks = split_by_sentences(text, skeleton, max_tokens=20)
    for chunk_text, _ in chunks:
        opens = len(re.findall(r"\[\[T\d+\]\]", chunk_text))
        closes = len(re.findall(r"\[\[/T\d+\]\]", chunk_text))
        assert opens == closes


def test_split_renumbers_placeholders_within_chunk() -> None:
    skeleton = [
        InlineToken(tag="em", kind="pair"),
        InlineToken(tag="strong", kind="pair"),
    ]
    text = (
        "Part one with [[T0]]emphasis[[/T0]]. Part two with [[T1]]strong[[/T1]] words."
    )
    chunks = split_by_sentences(text, skeleton, max_tokens=4)
    assert len(chunks) == 2
    first_text, first_skel = chunks[0]
    second_text, second_skel = chunks[1]
    assert "[[T0]]" in first_text and "[[/T0]]" in first_text
    assert "[[T1]]" not in first_text
    assert first_skel == [skeleton[0]]
    assert "[[T0]]" in second_text and "[[/T0]]" in second_text
    assert second_skel == [skeleton[1]]
