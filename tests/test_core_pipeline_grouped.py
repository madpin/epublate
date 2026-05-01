"""Tests for the grouped translation pipeline (small-segment batching).

The grouped path batches many short, placeholder-free segments into a
single LLM call to amortize cost and round-trip latency. The invariants
it must preserve:

* per-segment DB commits — each segment still gets its own ``llm_call``
  row and ``segment.translated`` event,
* cache keys are per-segment (so a second run finds cache hits for each
  one individually),
* if the batch response can't be parsed, the pipeline falls back to
  per-segment translation so the batch doesn't corrupt any segment.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from epublate.core.pipeline import (
    TranslateOptions,
    is_group_eligible,
    translate_segments_grouped,
)
from epublate.core.project import Project
from epublate.db import repo
from epublate.db.schema import SegmentStatus
from epublate.llm.base import Message
from epublate.llm.mock import MockLLMProvider


def _list_entries_chapter(
    tiny_factory: Callable[..., Path],
    tmp_path: Path,
    *,
    count: int = 6,
) -> Project:
    """Project whose single chapter is a <ul> of short entries (TOC shape)."""

    items = "".join(f"<li>Entry {i + 1}</li>" for i in range(count))
    html = f"<h1>Index</h1><ul>{items}</ul>"
    src = tiny_factory(chapters=[("Index", html)])
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def _pending_small_segments(project: Project) -> list[repo.SegmentRow]:
    rows = repo.list_segments_by_status(
        project.engine, project_id=project.project_id, status="pending"
    )
    return [row for row in rows if is_group_eligible(row)]


def _group_responder() -> Callable[[list[Message], str], str]:
    """Mock responder that returns a well-formed grouped JSON payload."""

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


def test_grouped_translate_uses_one_call_for_many_short_segments(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _list_entries_chapter(tiny_epub_factory, tmp_path, count=8)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_group_responder())

        segments = _pending_small_segments(project)
        assert len(segments) >= 6, "fixture should produce several list items"

        outcomes = translate_segments_grouped(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            style_guide=None,
            segments=segments,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )

        assert len(outcomes) == len(segments)
        assert provider.call_count == 1, "one batched LLM call covers all segments"
        assert all(o.extra.get("grouped") for o in outcomes)

        for seg, outcome in zip(segments, outcomes, strict=True):
            refreshed = repo.get_segment(project.engine, seg.id)
            assert refreshed is not None
            assert refreshed.status == SegmentStatus.TRANSLATED
            assert outcome.target_text == f"PT::{seg.source_text}"

        calls = repo.list_llm_calls(project.engine, project.project_id)
        translate_calls = [c for c in calls if c.purpose == "translate"]
        assert len(translate_calls) == len(segments), (
            "each segment still gets its own llm_call audit row"
        )
    finally:
        project.close()


def test_grouped_translate_replays_cache_per_segment(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _list_entries_chapter(tiny_epub_factory, tmp_path, count=4)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_group_responder())

        segments = _pending_small_segments(project)
        translate_segments_grouped(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            style_guide=None,
            segments=segments,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )
        assert provider.call_count == 1

        _reset_segments_to_pending(project, segments)

        refreshed = [
            repo.get_segment(project.engine, seg.id) or seg for seg in segments
        ]

        outcomes = translate_segments_grouped(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            style_guide=None,
            segments=refreshed,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )
        assert provider.call_count == 1, (
            "every segment was cached individually, so no new LLM call"
        )
        assert all(o.cache_hit for o in outcomes)
    finally:
        project.close()


def test_grouped_translate_falls_back_on_parse_failure(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _list_entries_chapter(tiny_epub_factory, tmp_path, count=3)
    try:
        provider = MockLLMProvider()

        call_state = {"group_seen": False}

        def _responder(messages: list[Message], _model: str) -> str:
            user_content = messages[-1].content
            if (
                user_content.startswith("{")
                and "items" in user_content
                and not call_state["group_seen"]
            ):
                call_state["group_seen"] = True
                return "not-a-parseable-response"
            return json.dumps(
                {
                    "target": f"PT::{user_content}",
                    "used_entries": [],
                    "new_entities": [],
                    "notes": None,
                }
            )

        provider.set_responder(_responder)

        segments = _pending_small_segments(project)
        outcomes = translate_segments_grouped(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            style_guide=None,
            segments=segments,
            provider=provider,
            options=TranslateOptions(model="gpt-mock"),
        )

        assert call_state["group_seen"], "group call must have been attempted"
        assert len(outcomes) == len(segments)
        assert all(not o.extra.get("grouped") for o in outcomes), (
            "fallback path should translate per segment without the grouped flag"
        )
        assert provider.call_count == 1 + len(segments), (
            "one failed group call + one per-segment call per item"
        )
        for seg, outcome in zip(segments, outcomes, strict=True):
            assert outcome.target_text == f"PT::{seg.source_text}"
            refreshed = repo.get_segment(project.engine, seg.id)
            assert refreshed is not None
            assert refreshed.status == SegmentStatus.TRANSLATED
    finally:
        project.close()


def _reset_segments_to_pending(
    project: Project,
    segments: list[repo.SegmentRow],
) -> None:
    """Flip each segment back to ``pending`` so the cache lookup
    actually fires on the second translate pass.

    The :func:`is_group_eligible` gate depends on status: without
    resetting, the helper would skip the grouped path entirely because
    the rows are already ``translated``.
    """

    with project.engine.begin() as conn:
        conn.execute(
            _update_status_sql(),
            [{"id": seg.id, "status": SegmentStatus.PENDING} for seg in segments],
        )


def _update_status_sql() -> Any:
    from sqlalchemy import text

    return text("UPDATE segment SET status = :status WHERE id = :id")
