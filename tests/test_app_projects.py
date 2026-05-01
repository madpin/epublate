"""Tests for the modern Projects landing screen (PRD §4.6)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from epublate.app.main import EpublateApp
from epublate.app.recents import RecentProject, RecentsStore
from epublate.app.screens.new_project import NewProjectModal
from epublate.app.screens.open_project import OpenProjectModal
from epublate.app.screens.projects import ProjectsScreen
from epublate.core.project import Project


@pytest.mark.asyncio
async def test_app_boots_into_projects_and_quits(tmp_path: Path) -> None:
    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        assert isinstance(pilot.app.screen, ProjectsScreen)
        await pilot.press("q")
    assert app.return_value is None


@pytest.mark.asyncio
async def test_default_model_plumbs_to_default_projects_screen() -> None:
    """``EpublateApp(default_model=...)`` reaches the auto-built screen.

    Guards the bare-``epublate`` TUI path against silently ignoring
    ``$EPUBLATE_LLM_MODEL`` (PRD F-LLM-1) — when the CLI doesn't pass an
    ``initial_screen`` we still want the resolved translator model to
    propagate to the Dashboard the user opens from there.
    """

    app = EpublateApp(default_model="gpt-5-free")
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProjectsScreen)
        assert screen._default_model == "gpt-5-free"
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_action_opens_new_project_modal(tmp_path: Path) -> None:
    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(pilot.app.screen, NewProjectModal)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ProjectsScreen)
        await pilot.press("q")


@pytest.mark.asyncio
async def test_open_action_opens_open_project_modal(tmp_path: Path) -> None:
    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("o")
        await pilot.pause()
        assert isinstance(pilot.app.screen, OpenProjectModal)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ProjectsScreen)
        await pilot.press("q")


@pytest.mark.asyncio
async def test_projects_table_loads_from_recents(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    project_dir = tmp_path / "demo"
    project = Project.create(
        sample_epub_path,
        out_dir=project_dir,
        source_lang="en",
        target_lang="pt",
        name="Demo Project",
    )
    project.close()

    recents = tmp_path / "recents.json"
    store = RecentsStore()
    store.upsert(
        RecentProject(
            project_dir=str(project_dir),
            name="Demo Project",
            source_lang="en",
            target_lang="pt",
            last_opened=time.time(),
        )
    )
    store.save(recents)

    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = pilot.app.screen
        assert isinstance(screen, ProjectsScreen)
        from textual.widgets import DataTable

        table = screen.query_one("#projects-table", DataTable)
        assert table.row_count == 1
        await pilot.press("q")


@pytest.mark.asyncio
async def test_refresh_prunes_missing_entries(tmp_path: Path) -> None:
    recents = tmp_path / "recents.json"
    store = RecentsStore()
    store.upsert(
        RecentProject(
            project_dir=str(tmp_path / "definitely-not-here"),
            name="Ghost",
            source_lang="en",
            target_lang="pt",
            last_opened=time.time(),
        )
    )
    store.save(recents)

    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("r")
        await pilot.pause()
        from textual.widgets import DataTable

        table = pilot.app.screen.query_one("#projects-table", DataTable)
        assert table.row_count == 0
        await pilot.press("q")

    rebuilt = RecentsStore.load(recents)
    assert rebuilt.entries == []


@pytest.mark.asyncio
async def test_remove_selected_drops_entry_from_store(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    project_dir = tmp_path / "demo"
    project = Project.create(
        sample_epub_path,
        out_dir=project_dir,
        source_lang="en",
        target_lang="pt",
        name="Demo Project",
    )
    project.close()

    recents = tmp_path / "recents.json"
    store = RecentsStore()
    store.upsert(
        RecentProject(
            project_dir=str(project_dir),
            name="Demo Project",
            source_lang="en",
            target_lang="pt",
            last_opened=time.time(),
        )
    )
    store.save(recents)

    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.pause()
        from textual.widgets import DataTable

        table = pilot.app.screen.query_one("#projects-table", DataTable)
        assert table.row_count == 1
        await pilot.press("delete")
        await pilot.pause()
        assert table.row_count == 0
        await pilot.press("q")

    assert RecentsStore.load(recents).entries == []
