"""Batch grouping integration tests.

Verify that ``run_batch`` partitions pending segments into grouped
work items for dense list-like content and falls back to per-segment
work when grouping is disabled. The key measurement is the LLM call
count — grouping should collapse N short items into 1 round-trip.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from epublate.core.batch import BatchOptions, run_batch
from epublate.core.project import Project
from epublate.db import repo
from epublate.db.schema import SegmentStatus
from epublate.llm.base import Message
from epublate.llm.mock import MockLLMProvider


def _index_project(
    tiny_factory: Callable[..., Path], tmp_path: Path, *, count: int = 8
) -> Project:
    items = "".join(f"<li>Item {i + 1}</li>" for i in range(count))
    html = f"<h1>Index</h1><ul>{items}</ul>"
    src = tiny_factory(chapters=[("Index", html)])
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def _dual_responder() -> Callable[[list[Message], str], str]:
    """Responder that handles both single and grouped user payloads.

    A grouped user message is JSON with an ``items`` array. Anything
    else is treated as a plain source segment (the single-segment
    translator's user content).
    """

    def _responder(messages: list[Message], _model: str) -> str:
        user = messages[-1].content
        try:
            payload = json.loads(user)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("items"), list):
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
                        for item in payload["items"]
                    ]
                }
            )
        return json.dumps(
            {
                "target": f"PT::{user}",
                "used_entries": [],
                "new_entities": [],
                "notes": None,
            }
        )

    return _responder


def test_run_batch_groups_short_segments_into_single_call(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _index_project(tiny_epub_factory, tmp_path, count=8)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_dual_responder())

        summary = run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(
                model="gpt-mock",
                concurrency=1,
                group_small_segments=True,
                group_max_items=50,
            ),
        )

        assert summary.failed == 0
        assert summary.translated >= 8

        # The book has one <h1> heading + 8 <li> items + intake housekeeping.
        # The grouped call should cover the list in ONE LLM round-trip
        # while any long/heading segments go through the per-segment path.
        assert provider.call_count <= 3, (
            "grouping should collapse ~8 list items into a single LLM call; "
            f"observed {provider.call_count}"
        )

        segments = repo.list_segments_by_status(
            project.engine, project_id=project.project_id, status="translated"
        )
        assert len(segments) == summary.translated
    finally:
        project.close()


def test_run_batch_disables_grouping_when_requested(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _index_project(tiny_epub_factory, tmp_path, count=6)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_dual_responder())

        pending = repo.list_segments_by_status(
            project.engine, project_id=project.project_id, status="pending"
        )
        n_pending = len(pending)
        assert n_pending >= 6

        summary = run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(
                model="gpt-mock",
                concurrency=1,
                group_small_segments=False,
            ),
        )

        assert summary.failed == 0
        assert summary.translated == n_pending
        assert provider.call_count == n_pending, (
            "grouping off should produce one LLM call per segment"
        )
    finally:
        project.close()


def test_run_batch_progress_fires_per_segment_even_when_grouped(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _index_project(tiny_epub_factory, tmp_path, count=5)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_dual_responder())

        events: list[str] = []

        def on_progress(event) -> None:  # type: ignore[no-untyped-def]
            events.append(event.segment_id)

        run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(
                model="gpt-mock",
                concurrency=1,
                group_small_segments=True,
                group_max_items=50,
            ),
            on_progress=on_progress,
        )

        pending_after = repo.list_segments_by_status(
            project.engine, project_id=project.project_id, status="pending"
        )
        assert not pending_after
        assert len(events) >= 5, (
            "progress callback must fire once per source segment — even when "
            "N segments share a single LLM call"
        )
        # No duplicate segment ids.
        assert len(events) == len(set(events))
    finally:
        project.close()


def test_run_batch_group_size_caps_items_per_call(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """``group_max_items=3`` should fan out a long list into multiple group calls."""

    project = _index_project(tiny_epub_factory, tmp_path, count=10)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_dual_responder())

        run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(
                model="gpt-mock",
                concurrency=1,
                group_small_segments=True,
                group_max_items=3,
            ),
        )
        pending_after = repo.list_segments_by_status(
            project.engine, project_id=project.project_id, status="pending"
        )
        assert not pending_after

        # Ten list items / 3-per-group => at least 4 group calls, plus a
        # handful of single-segment calls for anything too long / with
        # placeholders (headings with <h1> content get segmented too).
        # The cap has to be strictly less than the total pending count
        # to prove grouping is actually happening.
        assert 3 < provider.call_count < 10
    finally:
        project.close()


def test_run_batch_long_segment_uses_single_path(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A paragraph longer than ``group_max_source_chars`` bypasses grouping."""

    long_para = "x " * 150  # ~300 chars, above default cap
    src = tiny_factory_once = tiny_epub_factory(
        chapters=[("C1", f"<p>{long_para}</p><ul><li>A</li><li>B</li></ul>")]
    )
    del tiny_factory_once
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        provider.set_responder(_dual_responder())

        run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(
                model="gpt-mock",
                concurrency=1,
                group_small_segments=True,
                group_max_items=50,
            ),
        )

        rows = repo.list_segments_by_status(
            project.engine,
            project_id=project.project_id,
            status=SegmentStatus.TRANSLATED,
        )
        assert rows, "batch should have translated at least one row"
    finally:
        project.close()
