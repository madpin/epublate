"""Tests for the M2 single-segment translation pipeline (PRD §4.2)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.core.pipeline import (
    TranslateOptions,
    estimate_segment_tokens,
    translate_segment,
)
from epublate.core.project import Project
from epublate.db import repo
from epublate.db.schema import SegmentStatus
from epublate.errors import LLMResponseError
from epublate.formats.base import InlineToken
from epublate.llm.mock import MockLLMProvider


def _placeholder_preserving_target(source_text: str) -> str:
    """Return a deterministic 'translation' that round-trips placeholders."""

    return f"PT::{source_text}"


def _open_translatable_segment(
    project: Project,
) -> tuple[repo.ChapterRow, repo.SegmentRow]:
    """Pick the first segment that *has* inline tags, falling back to any."""

    chapters = repo.list_chapters(project.engine, project.project_id)
    assert chapters, "fixture project has no chapters"
    for chap in chapters:
        segs = repo.list_segments(project.engine, chap.id)
        for seg in segs:
            return chap, seg
    raise AssertionError("fixture project has no segments")


def test_translate_segment_persists_target_and_audits(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1>"
                "<p>Hello, <em>brave</em> world.</p>"
                '<p>Second with a <a href="#x">link</a>.</p>',
            )
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        provider.set_responder(
            lambda msgs, model: json.dumps(
                {
                    "target": _placeholder_preserving_target(msgs[-1].content),
                    "used_entries": [],
                    "new_entities": [],
                    "notes": None,
                }
            )
        )

        _chap, seg = _open_translatable_segment(project)
        outcome = translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=seg,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )

        assert outcome.cache_hit is False
        assert outcome.target_text == f"PT::{seg.source_text}"
        assert provider.call_count == 1

        refreshed = repo.get_segment(project.engine, seg.id)
        assert refreshed is not None
        assert refreshed.target_text == outcome.target_text
        assert refreshed.status == SegmentStatus.TRANSLATED

        calls = repo.list_llm_calls(project.engine, project.project_id)
        translate_calls = [c for c in calls if c.purpose == "translate"]
        assert len(translate_calls) == 1
        recorded = translate_calls[0]
        assert recorded.cache_hit is False
        assert recorded.cache_key == outcome.cache_key
        assert recorded.cost_usd == 0.0  # mock model is free in pricing table

        events = repo.list_events(project.engine, project.project_id)
        assert any(e.kind == "segment.translated" for e in events)
    finally:
        project.close()


def test_translate_segment_cache_hit_skips_provider(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        provider.set_responder(
            lambda msgs, model: json.dumps({"target": f"PT::{msgs[-1].content}"})
        )

        _chap, seg = _open_translatable_segment(project)
        opts = TranslateOptions(model="gpt-mock")

        first = translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=seg,
            provider=provider,
            options=opts,
        )
        assert provider.call_count == 1
        assert first.cache_hit is False

        second = translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=seg,
            provider=provider,
            options=opts,
        )
        assert provider.call_count == 1, "cache hit must not call the provider"
        assert second.cache_hit is True
        assert second.cache_key == first.cache_key
        assert second.target_text == first.target_text
        assert second.cost_usd == 0.0

        translate_calls = [
            c
            for c in repo.list_llm_calls(project.engine, project.project_id)
            if c.purpose == "translate"
        ]
        assert len(translate_calls) == 2
        assert translate_calls[0].cache_hit is False
        assert translate_calls[1].cache_hit is True
    finally:
        project.close()


def test_bypass_cache_re_invokes_provider(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        provider.queue_responses(
            [
                json.dumps({"target": "first attempt"}),
                json.dumps({"target": "retry result"}),
            ]
        )

        _open_translatable_segment(project)
        # First segment may have inline tags; switch to one without to keep
        # this test focused on caching, not placeholder math.
        chapters = repo.list_chapters(project.engine, project.project_id)
        candidate = None
        for chap in chapters:
            for s in repo.list_segments(project.engine, chap.id):
                if not s.inline_skeleton:
                    candidate = s
                    break
            if candidate is not None:
                break
        assert candidate is not None, "fixture lacks a no-inline segment"

        first = translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=candidate,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )
        assert first.target_text == "first attempt"

        second = translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=candidate,
            provider=provider,
            options=TranslateOptions(model="gpt-mock", bypass_cache=True),
        )
        assert second.target_text == "retry result"
        assert second.cache_key != first.cache_key
        assert provider.call_count == 2
    finally:
        project.close()


def test_invalid_translator_response_persists_audit_and_raises(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        provider.set_response("this is not even close to JSON")

        _chap, seg = _open_translatable_segment(project)
        with pytest.raises(LLMResponseError):
            translate_segment(
                engine=project.engine,
                project_id=project.project_id,
                source_lang="en",
                target_lang="pt",
                style_guide=None,
                segment=seg,
                provider=provider,
                options=TranslateOptions(model="gpt-mock"),
            )

        # Segment must remain untranslated.
        refreshed = repo.get_segment(project.engine, seg.id)
        assert refreshed is not None
        assert refreshed.target_text is None
        assert refreshed.status == SegmentStatus.PENDING

        # The audit row was still appended.
        calls = repo.list_llm_calls(project.engine, project.project_id)
        assert any(c.purpose == "translate" for c in calls)
        events = repo.list_events(project.engine, project.project_id)
        assert any(e.kind == "segment.translation_failed" for e in events)
    finally:
        project.close()


def test_pipeline_rejects_target_with_broken_placeholders(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory(
        chapters=[
            ("Solo", "<h1>Solo</h1><p>Hello, <em>brave</em> world.</p>"),
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        # Drop the closing placeholder — must be rejected by validator.
        provider.set_response(json.dumps({"target": "Olá [[T0]]bravo mundo."}))

        # Find a segment that *has* inline placeholders.
        target_seg: repo.SegmentRow | None = None
        for chap in repo.list_chapters(project.engine, project.project_id):
            for s in repo.list_segments(project.engine, chap.id):
                if s.inline_skeleton and any(
                    isinstance(t, InlineToken) and t.kind == "pair"
                    for t in s.inline_skeleton
                ):
                    target_seg = s
                    break
            if target_seg is not None:
                break
        assert target_seg is not None

        from epublate.errors import FormatError

        with pytest.raises(FormatError):
            translate_segment(
                engine=project.engine,
                project_id=project.project_id,
                source_lang="en",
                target_lang="pt",
                style_guide=None,
                segment=target_seg,
                provider=provider,
                options=TranslateOptions(model="gpt-mock"),
            )

        refreshed = repo.get_segment(project.engine, target_seg.id)
        assert refreshed is not None
        assert refreshed.target_text is None
    finally:
        project.close()


def test_estimate_segment_tokens_is_positive() -> None:
    n = estimate_segment_tokens(
        source_lang="en",
        target_lang="pt",
        source_text="Hello world.",
        style_guide=None,
    )
    assert n > 0
