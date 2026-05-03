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
    is_trivially_empty,
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


@pytest.mark.parametrize(
    "source",
    [
        "",
        " ",
        "\u00a0",  # NBSP from <p>&#160;</p>
        "\u00a0\u00a0\t\n",  # mixed whitespace
        "\u200b",  # zero-width space
        "\ufeff\u200b\u200d",  # BOM + zero-width family
        "[[T0]]",  # void placeholder, no text
        "[[T0]][[/T0]]",  # empty pair
        "[[T0]]\u00a0[[/T0]]",  # link wrapping NBSP only
        "  [[T0]]\u200b[[/T0]]\t",  # mix of whitespace + zero-width inside pair
    ],
)
def test_is_trivially_empty_catches_blank_segments(source: str) -> None:
    assert is_trivially_empty(source) is True


@pytest.mark.parametrize(
    "source",
    [
        "Hello",
        "1",
        "—",
        "[[T0]]Chapter 1[[/T0]]",
        "[[T0]]\u00a0Title[[/T0]]",
        "  word  ",
    ],
)
def test_is_trivially_empty_keeps_real_content(source: str) -> None:
    assert is_trivially_empty(source) is False


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


# ---------------------------------------------------------------------------
# Entity reference handling — guards the "Save ePub" / cyfunction Entity bug
# ---------------------------------------------------------------------------

ENTITY_DOCTYPE = (
    '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" '
    '"http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">'
)


def _parse_entity_paragraph(inner: str) -> etree._Element:
    """Parse a paragraph that contains XHTML named entity references.

    Real-world ePubs frequently inline ``&nbsp;`` / ``&copy;`` next to
    text and reference the XHTML 1.1 DTD in the chapter prologue.
    Without ``load_dtd=True`` (which we deliberately avoid for safety
    + reproducibility) lxml leaves those references as
    ``etree.Entity`` nodes inside the parsed tree.
    """

    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, recover=False
    )
    xml = (
        f'<?xml version="1.0"?>{ENTITY_DOCTYPE}<p xmlns="{XHTML_NS}">{inner}</p>'
    ).encode()
    return etree.fromstring(xml, parser)


def test_placeholderize_handles_entity_reference_round_trip() -> None:
    original = _parse_entity_paragraph("hello&nbsp;world")
    text, skeleton = placeholderize(original)

    assert len(skeleton) == 1
    assert skeleton[0].kind == "entity"
    assert skeleton[0].attrs.get("name") == "nbsp"
    assert "[[T0]]" in text and "[[/T0]]" not in text

    target = _parse_entity_paragraph("")
    apply_parts_to_host(target, [(text, skeleton)])
    assert _serialize(target) == _serialize(original)


def test_placeholderize_preserves_entity_alongside_inline_tags() -> None:
    """Mixing pair tags with entity refs round-trips both cleanly.

    Calibre-converted ePubs commonly produce ``<p>The
    <em>old</em>&nbsp;man saw &copy;Author.</p>`` shapes; the
    pre-fix code was crashing the Save ePub path on these.
    """

    original = _parse_entity_paragraph("The <em>old</em>&nbsp;man saw &copy;Author.")
    text, skeleton = placeholderize(original)

    kinds = [tok.kind for tok in skeleton]
    assert kinds == ["pair", "entity", "entity"]
    assert skeleton[1].attrs.get("name") == "nbsp"
    assert skeleton[2].attrs.get("name") == "copy"

    target = _parse_entity_paragraph("")
    apply_parts_to_host(target, [(text, skeleton)])
    assert _serialize(target) == _serialize(original)


def test_apply_parts_skips_legacy_cyfunction_tag() -> None:
    """Old segmenter runs persisted ``"<cyfunction Entity at 0x..>"`` tags.

    Re-running ``apply_parts_to_host`` on a freshly opened project
    must not crash on those legacy tokens — the curator should still
    be able to export their (possibly partial) translation. We
    silently skip the broken placeholder; the surrounding text is
    preserved.
    """

    target = _parse(_wrap_paragraph(""))
    text = "before [[T0]] after"
    legacy = InlineToken(tag="<cyfunction Entity at 0x108cefad0>", kind="void")
    apply_parts_to_host(target, [(text, [legacy])])
    rendered = _serialize(target)
    assert "before " in rendered
    assert " after" in rendered
    assert "<cyfunction" not in rendered


def test_placeholderize_skips_xml_comments() -> None:
    """XML comments inside a host don't end up in the segment.

    Comments are non-content nodes; sending them to the LLM (or
    asking the validator to round-trip them) just adds noise. We
    drop them from the placeholder text and the skeleton.
    """

    parser = etree.XMLParser(
        resolve_entities=False, no_network=True, load_dtd=False, recover=False
    )
    xml = (
        f'<?xml version="1.0"?><p xmlns="{XHTML_NS}">before<!--editor note-->after</p>'
    ).encode()
    original = etree.fromstring(xml, parser)
    text, skeleton = placeholderize(original)
    assert "[[T" not in text
    assert skeleton == []
    assert "before" in text
    assert "after" in text
