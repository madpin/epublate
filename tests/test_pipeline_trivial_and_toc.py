"""Pipeline tests for the trivial short-circuit + TOC-link grouping.

Two perf wins land here (PRD F-LLM-7 / F-LLM-9):

* **Trivial short-circuit.** Segments whose source is wholly placeholders
  + invisible glue (``&nbsp;``, BOM, zero-width spaces) skip the LLM
  entirely — they were costing one round-trip per ``<p>&#160;</p>``
  separator on Calibre-converted ePubs even though the result was
  always the source verbatim.
* **TOC / index grouping.** Segments wrapped in a single ``<a>`` link
  (the TOC / index shape) used to be excluded from the grouped path
  because they contain placeholders. They're now batched alongside
  plain list items, with a per-item placeholder cap and the standard
  fallback to per-segment translation if the response can't be parsed.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from epublate.core.pipeline import (
    GROUP_DEFAULT_MAX_PLACEHOLDERS,
    TranslateOptions,
    is_group_eligible,
    translate_segment,
    translate_segments_grouped,
)
from epublate.core.project import Project
from epublate.db import repo
from epublate.db.schema import SegmentStatus
from epublate.formats.base import InlineToken
from epublate.llm.base import Message
from epublate.llm.mock import MockLLMProvider


def _trivial_pending_segment(project: Project) -> repo.SegmentRow:
    """Force a trivially-empty segment into the project DB.

    Real segmentation already filters these out at intake (see
    :func:`epublate.formats.epub.EpubAdapter.segment`), so to exercise
    the pipeline-level short-circuit we slip one in directly via the
    repo. This mirrors what an old project would look like if it was
    segmented before the filter existed.
    """

    chapters = repo.list_chapters(project.engine, project.project_id)
    chapter = chapters[0]
    seg = repo.SegmentRow(
        id="trivial-test-seg",
        chapter_id=chapter.id,
        idx=999,
        source_text="\u00a0",
        source_hash="trivial",
        target_text=None,
        status=SegmentStatus.PENDING,
        inline_skeleton=[],
        host_path="/html/body/p[trivial]",
        host_part=0,
        host_total_parts=1,
    )
    repo.bulk_insert_segments(project.engine, [seg])
    fetched = repo.get_segment(project.engine, seg.id)
    assert fetched is not None
    return fetched


def test_translate_segment_short_circuits_nbsp_only_segment(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        provider.set_responder(
            lambda msgs, model: json.dumps({"target": "should not be called"})
        )

        seg = _trivial_pending_segment(project)
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

        assert provider.call_count == 0, "trivial segment must not hit the LLM provider"
        assert outcome.target_text == seg.source_text
        assert outcome.cost_usd == 0.0
        assert outcome.prompt_tokens == 0
        assert outcome.completion_tokens == 0
        assert outcome.extra.get("trivial") is True
        assert outcome.cache_hit is False
        assert outcome.llm_call_id == ""

        refreshed = repo.get_segment(project.engine, seg.id)
        assert refreshed is not None
        assert refreshed.status == SegmentStatus.TRANSLATED
        assert refreshed.target_text == seg.source_text

        calls = repo.list_llm_calls(project.engine, project.project_id)
        assert all(c.segment_id != seg.id for c in calls), (
            "no llm_call row should be written for trivial segments"
        )
        events = repo.list_events(project.engine, project.project_id)
        assert any(
            e.kind == "segment.translated_trivial"
            and e.payload.get("segment_id") == seg.id
            for e in events
        )
    finally:
        project.close()


def test_translate_segment_short_circuits_zero_width_only_segment(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """ZWSP / BOM / WORD JOINER aren't ``str.isspace`` true but still
    carry no translatable content; the trivial short-circuit must
    catch them too."""

    src = tiny_epub_factory()
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        provider.set_responder(lambda msgs, model: json.dumps({"target": "X"}))

        chapters = repo.list_chapters(project.engine, project.project_id)
        seg = repo.SegmentRow(
            id="zw-test-seg",
            chapter_id=chapters[0].id,
            idx=998,
            source_text="\ufeff\u200b\u200d",
            source_hash="zw",
            target_text=None,
            status=SegmentStatus.PENDING,
            inline_skeleton=[],
            host_path="/html/body/p[zw]",
            host_part=0,
            host_total_parts=1,
        )
        repo.bulk_insert_segments(project.engine, [seg])
        fetched = repo.get_segment(project.engine, seg.id)
        assert fetched is not None

        outcome = translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=fetched,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )

        assert provider.call_count == 0
        assert outcome.extra.get("trivial") is True
        assert outcome.target_text == fetched.source_text
    finally:
        project.close()


def test_translate_segment_keeps_real_one_char_segment(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A single-character segment like a chapter number ("1") still
    deserves a real translation; the short-circuit must not catch it."""

    src = tiny_epub_factory(
        chapters=[("Numbered", "<h1>1</h1><p>First paragraph.</p>")],
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        provider.set_responder(
            lambda msgs, model: json.dumps({"target": f"PT::{msgs[-1].content}"})
        )

        # Find the "1" heading segment.
        target: repo.SegmentRow | None = None
        for chap in repo.list_chapters(project.engine, project.project_id):
            for s in repo.list_segments(project.engine, chap.id):
                if s.source_text.strip() == "1":
                    target = s
                    break
            if target is not None:
                break
        assert target is not None, "fixture should produce a '1'-only heading"

        translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=target,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )
        assert provider.call_count == 1, "one-char real content still goes to the LLM"
    finally:
        project.close()


def _toc_chapter(
    tiny_factory: Callable[..., Path],
    tmp_path: Path,
    *,
    count: int = 6,
) -> Project:
    """Project whose first chapter is a TOC of ``<a>``-wrapped entries."""

    items = "".join(
        f'<li><a href="#ch{i}">Chapter {i}</a></li>' for i in range(1, count + 1)
    )
    html = f"<h1>Contents</h1><ul>{items}</ul>"
    src = tiny_factory(chapters=[("Contents", html)])
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def _link_pending_segments(project: Project) -> list[repo.SegmentRow]:
    """Pending segments shaped like ``[[T0]]Chapter N[[/T0]]`` (TOC links)."""

    rows = repo.list_segments_by_status(
        project.engine, project_id=project.project_id, status="pending"
    )
    out: list[repo.SegmentRow] = []
    for row in rows:
        if not is_group_eligible(row):
            continue
        if any(
            isinstance(t, InlineToken) and t.kind == "pair" for t in row.inline_skeleton
        ):
            out.append(row)
    return out


def test_is_group_eligible_now_accepts_toc_link_segments(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _toc_chapter(tiny_epub_factory, tmp_path, count=4)
    try:
        link_segs = _link_pending_segments(project)
        assert link_segs, "fixture must produce link-shaped pending segments"
        for seg in link_segs:
            assert "[[T0]]" in seg.source_text, (
                "link segments should carry placeholders"
            )
            assert is_group_eligible(seg) is True
    finally:
        project.close()


def test_is_group_eligible_rejects_heavy_markup(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A short paragraph with too many inline tags should still bypass
    grouping — beyond the placeholder cap, the per-segment prompt's
    richer context is the safer call."""

    inner = "".join(f"<em>{i}</em>" for i in range(GROUP_DEFAULT_MAX_PLACEHOLDERS + 2))
    src = tiny_epub_factory(chapters=[("Heavy", f"<p>{inner}</p>")])
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        rows = repo.list_segments_by_status(
            project.engine, project_id=project.project_id, status="pending"
        )
        # Only the markup-heavy paragraph is exercised here.
        target = next(
            (
                r
                for r in rows
                if r.source_text.count("[[") > GROUP_DEFAULT_MAX_PLACEHOLDERS
            ),
            None,
        )
        assert target is not None, "fixture must produce a markup-heavy segment"
        assert is_group_eligible(target) is False
    finally:
        project.close()


def _group_responder() -> Callable[[list[Message], str], str]:
    """Mock responder echoing each item's source verbatim into ``target``.

    Real models would translate the prose; ours just round-trips so we
    can assert on placeholder preservation independently of any
    translation correctness.
    """

    def _responder(messages: list[Message], _model: str) -> str:
        payload = json.loads(messages[-1].content)
        items = payload.get("items", [])
        return json.dumps(
            {
                "translations": [
                    {
                        "id": item["id"],
                        "target": f"PT::{item['source']}",
                        "used_entries": [],
                        "new_entities": [],
                        "notes": None,
                    }
                    for item in items
                ]
            }
        )

    return _responder


def test_grouped_translate_handles_toc_link_segments_in_one_call(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The whole TOC of <a>-wrapped entries collapses into one LLM call.

    This is the user-visible win: index/TOC entries used to translate
    one-by-one because the legacy ``is_group_eligible`` rejected any
    segment containing ``[[T``. With the placeholder cap they ride the
    grouped path like plain ``<li>`` entries do.
    """

    project = _toc_chapter(tiny_epub_factory, tmp_path, count=8)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_group_responder())

        link_segs = _link_pending_segments(project)
        assert len(link_segs) >= 6, "fixture should yield several TOC link segments"

        outcomes = translate_segments_grouped(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            style_guide=None,
            segments=link_segs,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )

        assert provider.call_count == 1, (
            "all TOC links should batch into a single LLM round-trip"
        )
        assert len(outcomes) == len(link_segs)
        for seg, outcome in zip(link_segs, outcomes, strict=True):
            assert outcome.target_text.startswith("PT::")
            # Placeholder pairs round-trip end-to-end so the reassembled
            # ePub keeps its <a href="..."> wrappers.
            assert outcome.target_text.count("[[T0]]") == seg.source_text.count(
                "[[T0]]"
            )
            assert outcome.target_text.count("[[/T0]]") == seg.source_text.count(
                "[[/T0]]"
            )
            refreshed = repo.get_segment(project.engine, seg.id)
            assert refreshed is not None
            assert refreshed.status == SegmentStatus.TRANSLATED
    finally:
        project.close()
