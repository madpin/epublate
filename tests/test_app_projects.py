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
async def test_app_quit_cancels_running_batch_first(tmp_path: Path) -> None:
    """Quitting while a batch is in flight signals the worker's cancel event.

    Curators who exit the app expect the translation to stop rather
    than burn tokens on a process that no longer has a UI. The
    contract is "graceful shutdown": already-committed segments stay
    durable (WAL + per-segment transactions), but no new LLM calls
    are submitted past the quit. We verify by parking a fake batch
    handle on the App and confirming ``action_quit`` flips its
    cancel event.
    """

    import threading

    from epublate.app.main import (
        BatchProgress as RealBatchProgress,
    )
    from epublate.app.main import (
        _BatchHandle,
    )

    class _FakeProject:
        project_id = "proj-x"

    cancel_event = threading.Event()
    handle = _BatchHandle(
        project=_FakeProject(),  # type: ignore[arg-type]
        cancel_event=cancel_event,
        chapter_count=1,
        total_segments=10,
        started_at=0.0,
    )

    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.pause()
        # Manually park the fake handle + active progress so action_quit
        # sees a "live" batch without us needing to spin up a real
        # worker thread (which would race the test loop).
        pilot.app._batch_handle = handle  # type: ignore[attr-defined]
        pilot.app.batch_progress = RealBatchProgress(  # type: ignore[attr-defined]
            active=True,
            project_id="proj-x",
            summary=None,
            total=10,
            chapter_count=1,
        )
        assert not cancel_event.is_set()
        await pilot.press("q")
    assert cancel_event.is_set(), (
        "action_quit must flip the batch's cancel event so the worker "
        "stops submitting new LLM calls before the app exits."
    )
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

        from epublate.app.screens.projects import RemoveRecentConfirmModal

        table = pilot.app.screen.query_one("#projects-table", DataTable)
        assert table.row_count == 1
        # Pressing ``delete`` no longer wipes the row immediately —
        # the modal demands an explicit confirm so muscle-memory
        # delete-key spam can't shred the recents list.
        await pilot.press("delete")
        await pilot.pause()
        assert isinstance(pilot.app.screen, RemoveRecentConfirmModal)
        # Cancel path leaves the row untouched.
        await pilot.press("escape")
        await pilot.pause()
        assert table.row_count == 1

        await pilot.press("delete")
        await pilot.pause()
        assert isinstance(pilot.app.screen, RemoveRecentConfirmModal)
        await pilot.press("y")
        await pilot.pause()
        assert table.row_count == 0
        # Project files must remain on disk — Remove only drops the
        # recents row.
        assert project_dir.exists()
        await pilot.press("q")

    assert RecentsStore.load(recents).entries == []


@pytest.mark.asyncio
async def test_delete_project_wipes_folder_after_typed_confirm(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    """Capital ``D`` is the destructive path: project files are gone.

    Guards three behaviors at once:

    * Cancelling leaves the project untouched.
    * The destructive button refuses to fire until the curator types
      the project name verbatim.
    * Confirming wipes both the recents row and the project folder.
    """

    from textual.widgets import DataTable, Input

    from epublate.app.screens.projects import DeleteProjectConfirmModal

    project_dir = tmp_path / "doomed"
    project = Project.create(
        sample_epub_path,
        out_dir=project_dir,
        source_lang="en",
        target_lang="pt",
        name="Doomed Project",
    )
    project.close()
    assert project_dir.is_dir()

    recents = tmp_path / "recents.json"
    store = RecentsStore()
    store.upsert(
        RecentProject(
            project_dir=str(project_dir),
            name="Doomed Project",
            source_lang="en",
            target_lang="pt",
            last_opened=time.time(),
        )
    )
    store.save(recents)

    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.pause()
        table = pilot.app.screen.query_one("#projects-table", DataTable)
        assert table.row_count == 1

        # Cancel path keeps the folder + row.
        await pilot.press("D")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DeleteProjectConfirmModal)
        await pilot.press("escape")
        await pilot.pause()
        assert project_dir.is_dir()
        assert table.row_count == 1

        # Typed confirmation enables the destructive submit.
        await pilot.press("D")
        await pilot.pause()
        assert isinstance(pilot.app.screen, DeleteProjectConfirmModal)
        confirm_input = pilot.app.screen.query_one("#delete-confirm-input", Input)
        confirm_input.value = "Doomed Project"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert table.row_count == 0
        assert not project_dir.exists()
        await pilot.press("q")

    assert RecentsStore.load(recents).entries == []
