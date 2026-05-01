"""Tests for the M4 stats aggregator (PRD F-T-2)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from epublate.core.batch import BatchOptions, run_batch
from epublate.core.project import Project
from epublate.core.stats import (
    ALERT_KINDS,
    compute_chapter_shapes,
    compute_spend,
    compute_stats,
    flagged_segments,
    pending_segments,
    recent_alerts,
    summarize_chapter_shapes,
)
from epublate.db import repo
from epublate.db.schema import SegmentStatus
from epublate.llm.mock import MockLLMProvider


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(
        chapters=[
            (
                "C1",
                "<h1>C1</h1><p>Hello, world.</p><p>Second paragraph.</p>",
            ),
            (
                "C2",
                "<h1>C2</h1><p>Third paragraph.</p>",
            ),
        ]
    )
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def _placeholder_responder() -> Callable[..., str]:
    def _responder(messages: list[object], _model: str) -> str:
        last = messages[-1]
        content = getattr(last, "content", "")
        return json.dumps({"target": f"PT::{content}"})

    return _responder


def test_compute_stats_returns_zero_for_fresh_project(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        s = compute_stats(project.engine, project.project_id)
        assert s.spend_usd == 0.0
        assert s.llm_calls == 0
        assert s.cache_hits == 0
        assert s.cache_hit_rate == 0.0
        assert s.budget_usd is None
        assert s.segment_count > 0
        assert s.translated_count == 0
        assert s.progress_ratio == 0.0
        # All segments start pending.
        assert s.segments_by_status.get(SegmentStatus.PENDING) == s.segment_count
    finally:
        project.close()


def test_compute_stats_after_batch(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_responder())
        run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(model="gpt-mock"),
        )

        s = compute_stats(project.engine, project.project_id)
        assert s.llm_calls > 0
        # Mock model is free in the pricing table.
        assert s.spend_usd == 0.0
        assert s.translated_count == s.segment_count
        assert s.cache_hit_rate == 0.0
        assert s.spend_by_model == {"gpt-mock": 0.0}
        assert SegmentStatus.TRANSLATED in s.segments_by_status

        # Spend helper agrees with stats.
        assert compute_spend(project.engine, project.project_id) == 0.0
    finally:
        project.close()


def test_pending_segments_excludes_translated_rows(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        before = pending_segments(project.engine, project_id=project.project_id)
        assert before, "fresh project has pending segments"

        # Mark one segment as translated manually so we can verify the filter.
        repo.update_segment_translation(
            project.engine,
            segment_id=before[0].id,
            target_text="PT::manual",
            status=SegmentStatus.TRANSLATED,
        )
        after = pending_segments(project.engine, project_id=project.project_id)
        assert len(after) == len(before) - 1
        assert all(seg.id != before[0].id for seg in after)
    finally:
        project.close()


def test_recent_alerts_filters_by_kind(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        repo.append_event(
            project.engine,
            project_id=project.project_id,
            kind="batch.completed",
            payload={"translated": 2},
        )
        repo.append_event(
            project.engine,
            project_id=project.project_id,
            kind="segment.translated",  # NOT in ALERT_KINDS
            payload={"segment_id": "x"},
        )
        repo.append_event(
            project.engine,
            project_id=project.project_id,
            kind="entity.proposed",
            payload={"source_term": "Élise"},
        )

        alerts = recent_alerts(project.engine, project_id=project.project_id, limit=10)
        kinds = [a.kind for a in alerts]
        assert "batch.completed" in kinds
        assert "entity.proposed" in kinds
        assert "segment.translated" not in kinds
        for a in alerts:
            assert a.kind in ALERT_KINDS

        # Newest-first ordering.
        assert alerts[0].id is not None
        assert alerts[-1].id is not None
        assert alerts[0].id >= alerts[-1].id
    finally:
        project.close()


def test_chapter_shapes_summarizes_book_structure(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """``compute_chapter_shapes`` + ``summarize_chapter_shapes`` deliver the
    pre-flight stats the BatchModal renders: per-chapter counts plus the
    longest/shortest/median across the translatable chapters."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        shapes = compute_chapter_shapes(project.engine, project_id=project.project_id)
        assert shapes, "fixture should produce at least one chapter"
        for shape in shapes:
            assert shape.translatable_count >= 0
            assert shape.translated_count >= 0
            assert shape.approved_count >= 0
            assert shape.translatable_count >= shape.translated_count
            assert shape.pending_count == (
                shape.translatable_count - shape.translated_count
            )

        summary = summarize_chapter_shapes(shapes)
        translatable = [s for s in shapes if s.translatable_count > 0]
        assert summary.translatable_chapters == len(translatable)
        if translatable:
            assert summary.longest is not None
            assert summary.shortest is not None
            assert summary.longest.translatable_count == max(
                s.translatable_count for s in translatable
            )
            assert summary.shortest.translatable_count == min(
                s.translatable_count for s in translatable
            )
            assert summary.average_segments > 0.0
            assert summary.median_segments > 0.0
    finally:
        project.close()


def test_flagged_segments_returns_only_flagged(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        all_segs: list[repo.SegmentRow] = []
        for chap in repo.list_chapters(project.engine, project.project_id):
            all_segs.extend(repo.list_segments(project.engine, chap.id))
        repo.update_segment_translation(
            project.engine,
            segment_id=all_segs[0].id,
            target_text="bad",
            status=SegmentStatus.FLAGGED,
        )

        flagged = flagged_segments(project.engine, project_id=project.project_id)
        assert len(flagged) == 1
        assert flagged[0].id == all_segs[0].id
    finally:
        project.close()
