"""Tests for :mod:`epublate.core.style_sniff` (PRD F-STYLE-4)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.core.style_sniff import (
    DEFAULT_SAMPLE_STRATEGY,
    SampleStrategy,
    ToneSniff,
    sniff_tone,
)
from epublate.errors import FormatError, LLMResponseError
from epublate.formats.base import Segment
from epublate.llm.base import Message, ResponseFormat
from epublate.llm.mock import MockLLMProvider

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _wrap(extras: dict[str, object]) -> str:
    """Render an ExtractorTrace-shaped JSON payload the parser accepts."""

    body: dict[str, object] = {
        "entities": [],
        "pov": "",
        "tense": "",
        "register": None,
        "audience": None,
        "notes": "",
    }
    body.update(extras)
    return json.dumps(body)


def _picture_book_chapter() -> str:
    return (
        "<h1>Goodnight, Little One</h1>"
        "<p>The bunny hopped, hopped, hopped to the moon. "
        "She waved at the stars. <em>Twinkle</em> said the sky.</p>"
        "<p>Mama bunny said, “Time for bed, little one.”</p>"
    )


def _adult_chapter() -> str:
    return (
        "<h1>Chapter 1</h1>"
        "<p>The light bent against the cliff face like a tired witness, "
        "and the man — who had not been a man for very long — leaned into it.</p>"
        "<p>He had agreed, in his way, to remain.</p>"
    )


# ---------------------------------------------------------------------------
# sniff_tone happy paths
# ---------------------------------------------------------------------------


def test_sniff_tone_picks_children_picture_for_children_audience(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    epub_path = tiny_epub_factory([("Bunny", _picture_book_chapter())], name="picture")
    provider = MockLLMProvider()
    provider.set_response(_wrap({"register": "playful", "audience": "children"}))

    sniff = sniff_tone(
        epub_path=epub_path,
        provider=provider,
        helper_model="helper-model",
        source_lang="en",
        target_lang="pt",
    )

    assert isinstance(sniff, ToneSniff)
    assert sniff.profile == "children_picture"
    assert sniff.register == "playful"
    assert sniff.audience == "children"
    assert sniff.has_suggestion is True
    assert sniff.sample_block_count >= 1
    assert sniff.sample_chars > 0
    assert sniff.prompt_tokens > 0
    assert sniff.completion_tokens > 0
    assert sniff.cost_usd >= 0.0
    assert sniff.model == "helper-model"


def test_sniff_tone_falls_back_to_literary_for_literary_adult(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    epub_path = tiny_epub_factory([("Witness", _adult_chapter())], name="literary")
    provider = MockLLMProvider()
    provider.set_response(_wrap({"register": "literary", "audience": "adult"}))

    sniff = sniff_tone(
        epub_path=epub_path,
        provider=provider,
        helper_model="helper-model",
        source_lang="en",
        target_lang="pt",
    )

    assert sniff.profile == "literary_fiction"


def test_sniff_tone_returns_no_suggestion_when_helper_silent(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    epub_path = tiny_epub_factory([("Untitled", _adult_chapter())], name="silent")
    provider = MockLLMProvider()
    provider.set_response(_wrap({}))

    sniff = sniff_tone(
        epub_path=epub_path,
        provider=provider,
        helper_model="helper-model",
        source_lang="en",
        target_lang="pt",
    )

    assert sniff.profile is None
    assert sniff.register is None
    assert sniff.audience is None
    assert sniff.has_suggestion is False


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_sniff_tone_raises_format_error_when_path_missing(tmp_path: Path) -> None:
    provider = MockLLMProvider()
    with pytest.raises(FormatError):
        sniff_tone(
            epub_path=tmp_path / "missing.epub",
            provider=provider,
            helper_model="helper-model",
            source_lang="en",
            target_lang="pt",
        )


def test_sniff_tone_handles_minimal_book(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    """Even a body of only ``<script>`` succeeds — ebooklib's auto-nav
    contributes a translatable block (the book title + chapter title),
    which is enough for the helper to glance at register/audience.

    Documents the happy fallback so a future refactor that strips the
    nav can decide whether to special-case empty books."""

    epub_path = tiny_epub_factory(
        [("Boilerplate", "<script>console.log('skip')</script>")],
        name="boilerplate",
    )
    provider = MockLLMProvider()
    # Register/audience the suggester explicitly chooses not to map.
    provider.set_response(_wrap({"register": "experimental", "audience": "scholar"}))

    sniff = sniff_tone(
        epub_path=epub_path,
        provider=provider,
        helper_model="helper-model",
        source_lang="en",
        target_lang="pt",
    )

    assert sniff.sample_block_count >= 1
    # Unmapped register/audience → suggester returns None (the modal then
    # leaves the curator's current pick in place).
    assert sniff.profile is None
    assert sniff.register == "experimental"
    assert sniff.audience == "scholar"


def test_build_sample_text_rejects_blocks_with_only_placeholders(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    """Direct check on the lower helper: a list of placeholder-only
    blocks must raise rather than send an empty payload to the helper.
    Defends the invariant the modal counts on (no useful text → skip)."""

    from epublate.core.style_sniff import _build_sample_text

    placeholder_only = Segment(
        id="x",
        chapter_id="ch",
        idx=0,
        source_text="[[T0]][[/T0]]",
        source_hash="h",
        host_path="/x",
    )

    with pytest.raises(ValueError):
        _build_sample_text([placeholder_only], strategy=DEFAULT_SAMPLE_STRATEGY)


def test_sniff_tone_propagates_parser_errors(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    epub_path = tiny_epub_factory(name="garbage")
    provider = MockLLMProvider()
    provider.set_response("not json at all")

    with pytest.raises(LLMResponseError):
        sniff_tone(
            epub_path=epub_path,
            provider=provider,
            helper_model="helper-model",
            source_lang="en",
            target_lang="pt",
        )


# ---------------------------------------------------------------------------
# Sample strategy
# ---------------------------------------------------------------------------


def test_sniff_tone_threads_strategy_into_block_count(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    paragraphs = "".join(f"<p>Block {i}.</p>" for i in range(40))
    epub_path = tiny_epub_factory([("Long", paragraphs)], name="long")
    provider = MockLLMProvider()
    provider.set_response(_wrap({"register": "literary", "audience": "adult"}))

    sniff = sniff_tone(
        epub_path=epub_path,
        provider=provider,
        helper_model="helper-model",
        source_lang="en",
        target_lang="pt",
        sample_strategy=SampleStrategy(head=2, middle=1, tail=1),
    )

    # We requested at most 4 blocks; the book has 40 → expect ≤ 4 blocks.
    assert sniff.sample_block_count <= 4
    assert sniff.sample_block_count >= 1


def test_default_strategy_prefers_head() -> None:
    s = DEFAULT_SAMPLE_STRATEGY
    assert s.head >= s.middle
    assert s.head >= s.tail
    assert s.total_blocks == s.head + s.middle + s.tail


# ---------------------------------------------------------------------------
# Helper sees the right messages
# ---------------------------------------------------------------------------


def test_sniff_tone_calls_helper_with_extractor_messages(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    epub_path = tiny_epub_factory(
        [("Inspect", _picture_book_chapter())], name="inspect"
    )
    provider = MockLLMProvider()
    provider.set_response(_wrap({"register": "playful", "audience": "children"}))

    sniff_tone(
        epub_path=epub_path,
        provider=provider,
        helper_model="helper-model",
        source_lang="en",
        target_lang="pt",
    )

    assert provider.call_count == 1
    call = provider.last_request
    assert call is not None
    assert call.model == "helper-model"
    assert call.temperature == 0.0
    assert call.seed == 7
    # JSON mode mirrors the regular extractor's default — without it,
    # reasoning helpers like ``gpt-oss-20b`` return empty content and
    # the modal silently shows "no suggestion".
    assert call.response_format == ResponseFormat(type="json_object")
    # We expect the sniff to send an extractor-shaped pair (system + user).
    roles = [msg.role for msg in call.messages]
    assert roles == ["system", "user"]
    # User message must include the sample text and not raw [[T0]] noise.
    user_msg: Message = call.messages[1]
    assert "Block 0." not in user_msg.content  # this fixture has no Block 0
    assert "[[T0]]" not in user_msg.content


def test_sniff_tone_strips_placeholders_from_payload(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    chapter = (
        "<p>Once upon a time there was a <em>brave</em> bunny "
        'with a <a href="#fn1">footnote</a>.</p>'
    )
    epub_path = tiny_epub_factory([("Bunny", chapter)], name="placeholders")
    provider = MockLLMProvider()
    provider.set_response(_wrap({"register": "playful", "audience": "children"}))

    sniff_tone(
        epub_path=epub_path,
        provider=provider,
        helper_model="helper-model",
        source_lang="en",
        target_lang="pt",
    )

    user_text = provider.last_request.messages[1].content  # type: ignore[union-attr]
    assert "[[T0]]" not in user_text
    assert "[[/T0]]" not in user_text
    assert "brave" in user_text
    assert "footnote" in user_text


# ---------------------------------------------------------------------------
# ToneSniff dataclass invariants
# ---------------------------------------------------------------------------


def test_tone_sniff_is_frozen() -> None:
    sniff = ToneSniff(
        profile="literary_fiction",
        register="literary",
        audience="adult",
        sample_block_count=4,
        sample_chars=120,
        prompt_tokens=10,
        completion_tokens=5,
        cost_usd=0.0001,
        model="helper-model",
    )
    with pytest.raises((AttributeError, TypeError)):
        sniff.profile = "children_picture"  # type: ignore[misc]


def test_segment_dataclass_used_for_blocks(
    tiny_epub_factory: Callable[..., Path],
) -> None:
    """Smoke check that we're feeding the helper :class:`Segment` text.

    Defends against a future refactor that bypasses the segmenter — the
    sniff has to share the placeholderizer with the production pipeline
    or it'd be lying about the format the LLM eventually sees.
    """

    from epublate.core.style_sniff import _collect_translatable_blocks

    epub_path = tiny_epub_factory()
    blocks = _collect_translatable_blocks(epub_path, chapter_max_tokens=800)
    assert blocks
    assert all(isinstance(b, Segment) for b in blocks)
