"""Pilot tests for the M4 Inbox screen (PRD §4.6)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.main import EpublateApp
from epublate.app.screens.inbox import InboxScreen
from epublate.app.screens.reader import ReaderScreen
from epublate.core.project import Project
from epublate.db import repo
from epublate.db.schema import SegmentStatus
from epublate.llm.mock import MockLLMProvider


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1><p>Hello, world.</p><p>Second paragraph.</p>",
            )
        ]
    )
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def _flag_first_segment(project: Project) -> repo.SegmentRow:
    chap = repo.list_chapters(project.engine, project.project_id)[0]
    seg = repo.list_segments(project.engine, chap.id)[0]
    repo.update_segment_translation(
        project.engine,
        segment_id=seg.id,
        target_text="bad",
        status=SegmentStatus.FLAGGED,
    )
    return seg


def _seed_proposed(project: Project) -> str:
    entry = repo.create_glossary_entry(
        project.engine,
        project_id=project.project_id,
        source_term="Élise",
        target_term="Élise",
        type="character",
        status="proposed",
    )
    return entry.id


def _seed_alert(project: Project) -> None:
    repo.append_event(
        project.engine,
        project_id=project.project_id,
        kind="batch.completed",
        payload={"translated": 3, "cached": 0, "flagged": 0, "cost_usd": 0.1234},
    )


@pytest.mark.asyncio
async def test_inbox_lists_all_three_streams(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _flag_first_segment(project)
        _seed_proposed(project)
        _seed_alert(project)

        provider = MockLLMProvider()
        screen = InboxScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, InboxScreen)
            kinds = {row.kind for row in current.rows}
            assert "flagged" in kinds
            assert "proposed" in kinds
            assert "alert" in kinds
    finally:
        project.close()


@pytest.mark.asyncio
async def test_inbox_filter_cycle_narrows_view(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _flag_first_segment(project)
        _seed_proposed(project)

        provider = MockLLMProvider()
        screen = InboxScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, InboxScreen)
            assert current.filter_value == "all"
            await pilot.press("f")
            await pilot.pause()
            assert current.filter_value == "flagged"
            await pilot.press("f")
            await pilot.pause()
            assert current.filter_value == "proposed"
    finally:
        project.close()


@pytest.mark.asyncio
async def test_inbox_enter_on_flagged_pushes_reader(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        seg = _flag_first_segment(project)

        provider = MockLLMProvider()
        screen = InboxScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ReaderScreen)
            current_seg = pilot.app.screen._current_segment()  # type: ignore[reportPrivateUsage]
            assert current_seg is not None
            assert current_seg.id == seg.id
    finally:
        project.close()


@pytest.mark.asyncio
async def test_inbox_refresh_picks_up_new_alerts(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = InboxScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, InboxScreen)
            initial_alert_count = sum(1 for r in current.rows if r.kind == "alert")

            _seed_alert(project)
            await pilot.press("r")
            await pilot.pause()

            new_alert_count = sum(1 for r in current.rows if r.kind == "alert")
            assert new_alert_count == initial_alert_count + 1
    finally:
        project.close()
