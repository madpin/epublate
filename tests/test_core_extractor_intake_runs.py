"""``run_book_intake`` / ``run_pre_pass`` must land an ``intake_run`` row.

The terminal ``event`` rows still carry the per-run audit trail (those
assertions live in ``tests/test_core_extractor.py``); the extra writes
verified here cover the persistent, editable record introduced for the
Intake history screen (PRD §4.3 / §7.1).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.core.extractor import (
    IntakeOptions,
    run_book_intake,
    run_pre_pass,
)
from epublate.core.project import Project
from epublate.db import repo
from epublate.db.schema import IntakeRunKind, IntakeRunStatus
from epublate.llm.mock import MockLLMProvider


def _extractor_response(*entities: dict[str, object], **kwargs: object) -> str:
    payload: dict[str, object] = {"entities": list(entities)}
    payload.update(kwargs)
    return json.dumps(payload)


@pytest.fixture
def fixture_project(tiny_epub_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1>"
                "<p>Élise opened the door of Vale Verde.</p>"
                "<p>The Order of the Coffer watched silently.</p>",
            )
        ]
    )
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def test_run_book_intake_persists_completed_intake_run(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        provider = MockLLMProvider()
        provider.set_responder(
            lambda msgs, model: _extractor_response(
                {"type": "character", "source": "Élise"},
                {"type": "place", "source": "Vale Verde"},
                pov="third_limited",
                register="literary",
                audience="adult",
                notes="watch out for honorifics",
            )
        )

        summary = run_book_intake(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(model="gpt-mock", max_segments=5),
        )

        rows = repo.list_intake_runs(project.engine, project_id=project.project_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.kind == IntakeRunKind.BOOK_INTAKE
        assert row.status == IntakeRunStatus.COMPLETED
        assert row.helper_model == "gpt-mock"
        assert row.chapter_id is None
        assert row.chunks == summary.chunks
        assert row.proposed_count == summary.proposed_count
        assert row.pov == "third_limited"
        assert row.narrative_register == "literary"
        assert row.audience == "adult"
        # Helper notes round-trip via the JSON-encoded ``notes`` column.
        assert row.notes
        assert summary.proposed_entry_ids
        assert sorted(row.proposed_entry_ids) == sorted(summary.proposed_entry_ids)
    finally:
        project.close()


def test_run_book_intake_persists_zero_segment_completion(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        provider = MockLLMProvider()
        run_book_intake(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(model="gpt-mock", max_segments=0),
        )
        rows = repo.list_intake_runs(project.engine, project_id=project.project_id)
        assert len(rows) == 1
        assert rows[0].status == IntakeRunStatus.COMPLETED
        assert rows[0].chunks == 0
    finally:
        project.close()


def test_run_pre_pass_persists_chapter_pre_pass_with_chapter_id(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        chapters = repo.list_chapters(project.engine, project.project_id)
        segments = repo.list_segments(project.engine, chapters[0].id)

        provider = MockLLMProvider()
        provider.set_response(
            _extractor_response({"type": "place", "source": "Vale Verde"})
        )

        summary = run_pre_pass(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(model="gpt-mock"),
            segments=segments,
        )

        rows = repo.list_intake_runs(project.engine, project_id=project.project_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.kind == IntakeRunKind.CHAPTER_PRE_PASS
        assert row.chapter_id == chapters[0].id
        assert row.status == IntakeRunStatus.COMPLETED
        assert row.proposed_count == summary.proposed_count
        assert sorted(row.proposed_entry_ids) == sorted(summary.proposed_entry_ids)
    finally:
        project.close()


def test_run_pre_pass_persists_empty_segment_short_circuit(
    fixture_project: Project,
) -> None:
    project = fixture_project
    try:
        provider = MockLLMProvider()
        run_pre_pass(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(model="gpt-mock"),
            segments=[],
        )
        rows = repo.list_intake_runs(project.engine, project_id=project.project_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.kind == IntakeRunKind.CHAPTER_PRE_PASS
        assert row.status == IntakeRunStatus.COMPLETED
        assert row.chapter_id is None  # nothing to chapter-anchor on
        assert row.chunks == 0
    finally:
        project.close()


def test_run_pre_pass_persists_aborted_run(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """When the circuit breaker trips on a broken helper, the row is
    saved as ``aborted`` with the truncated last error string."""

    paragraphs = "".join(f"<p>Paragraph {i} of the loop.</p>" for i in range(8))
    src = tiny_epub_factory(chapters=[("Loop", "<h1>Loop</h1>" + paragraphs)])
    project = Project.create(
        src, out_dir=tmp_path / "loop", source_lang="en", target_lang="pt"
    )
    try:
        all_chapters = repo.list_chapters(project.engine, project.project_id)
        per_chapter = [
            (c, repo.list_segments(project.engine, c.id)) for c in all_chapters
        ]
        chapter, segments = max(per_chapter, key=lambda pair: len(pair[1]))
        assert len(segments) >= 4

        provider = MockLLMProvider()
        # Empty visible content trips the parser ("extractor response was
        # empty") on every chunk; with failure_streak_limit=2 the
        # breaker fires after the 2nd consecutive failure.
        provider.set_responder(lambda _msgs, _model: "")

        run_pre_pass(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            provider=provider,
            options=IntakeOptions(
                model="gpt-mock",
                chunk_max_tokens=5,
                failure_streak_limit=2,
            ),
            segments=segments,
        )

        rows = repo.list_intake_runs(project.engine, project_id=project.project_id)
        assert len(rows) == 1
        row = rows[0]
        assert row.kind == IntakeRunKind.CHAPTER_PRE_PASS
        assert row.status == IntakeRunStatus.ABORTED
        assert row.chapter_id == chapter.id
        assert row.error is not None and "empty" in row.error
    finally:
        project.close()
