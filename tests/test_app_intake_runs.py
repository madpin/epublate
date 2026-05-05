"""Smoke tests for the Intake history screen (PRD §4.3 / §7.1).

The detailed list rendering, the curator-notes round-trip, and the
"open detail" path are the user-visible surface of the new
``intake_run`` table — one assert per surface keeps the suite cheap
while still catching the obvious regressions (column counts / row
count / save-roundtrip).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from textual.widgets import DataTable, TextArea

from epublate.app.main import EpublateApp
from epublate.app.screens.intake_runs import (
    IntakeRunDetailModal,
    IntakeRunsScreen,
)
from epublate.core.project import Project
from epublate.db import repo
from epublate.db.schema import IntakeRunKind, IntakeRunStatus


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(chapters=[("Solo", "<p>Hello.</p>")])
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def _seed_run(
    project: Project,
    *,
    kind: str = IntakeRunKind.BOOK_INTAKE,
    chapter_id: str | None = None,
    proposed_count: int = 0,
    cost_usd: float = 0.0,
    notes: list[str] | None = None,
    suggested_style_profile: str | None = None,
    curator_notes: str | None = None,
    started_at: int = 1_700_000_000,
) -> repo.IntakeRunRow:
    return repo.record_intake_run(
        project.engine,
        project_id=project.project_id,
        kind=kind,
        chapter_id=chapter_id,
        helper_model="gpt-cheap-helper",
        started_at=started_at,
        finished_at=started_at + 10,
        status=IntakeRunStatus.COMPLETED,
        chunks=2,
        cached_chunks=0,
        proposed_count=proposed_count,
        failed_chunks=0,
        prompt_tokens=120,
        completion_tokens=80,
        cost_usd=cost_usd,
        pov="third_limited",
        tense="past",
        narrative_register="literary",
        audience="adult",
        suggested_style_profile=suggested_style_profile,
        notes=notes,
        curator_notes=curator_notes,
    )


@pytest.mark.asyncio
async def test_intake_runs_screen_lists_recorded_runs(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The DataTable lists one row per persisted intake run."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        chapters = repo.list_chapters(project.engine, project.project_id)
        first_chapter = chapters[0]

        _seed_run(
            project,
            kind=IntakeRunKind.BOOK_INTAKE,
            cost_usd=0.0125,
            proposed_count=2,
        )
        _seed_run(
            project,
            kind=IntakeRunKind.CHAPTER_PRE_PASS,
            chapter_id=first_chapter.id,
            cost_usd=0.005,
            proposed_count=1,
        )

        screen = IntakeRunsScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, IntakeRunsScreen)
            table = current.query_one("#intake-runs-table", DataTable)
            assert table.row_count == 2
    finally:
        project.close()


@pytest.mark.asyncio
async def test_intake_runs_screen_empty_state_advertises_intake_action(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """With no runs, the screen tells the curator how to create one."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = IntakeRunsScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, IntakeRunsScreen)
            table = current.query_one("#intake-runs-table", DataTable)
            assert table.row_count == 0
            status = current.query_one("#intake-runs-status")
            assert "No intake runs yet" in str(status.render())
    finally:
        project.close()


@pytest.mark.asyncio
async def test_intake_run_detail_modal_saves_curator_notes(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Editing the curator notes and pressing Ctrl+S persists them."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        run = _seed_run(
            project,
            notes=["watch out for honorifics"],
            suggested_style_profile="literary_modern",
        )

        modal = IntakeRunDetailModal(project, run)
        app = EpublateApp(initial_screen=modal)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, IntakeRunDetailModal)
            text_widget = current.query_one("#intake-detail-notes", TextArea)
            text_widget.text = "decided to keep the proposed Vale Verde."
            await pilot.press("ctrl+s")
            await pilot.pause()

        refreshed = repo.get_intake_run(project.engine, run.id)
        assert refreshed is not None
        assert refreshed.curator_notes == ("decided to keep the proposed Vale Verde.")
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_i_binding_pushes_intake_runs_screen(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Pressing ``I`` on the Dashboard opens Intake history (PRD §4.3)."""

    from epublate.app.screens.dashboard import DashboardScreen
    from epublate.llm.mock import MockLLMProvider

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(project, provider_factory=MockLLMProvider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("I")
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, IntakeRunsScreen)
    finally:
        project.close()
