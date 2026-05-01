"""End-to-end tests for the in-TUI new/open project modals."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from epublate.app.main import EpublateApp
from epublate.app.recents import RecentProject, RecentsStore
from epublate.app.screens.dashboard import DashboardScreen
from epublate.app.screens.file_browser import FileBrowserModal
from epublate.app.screens.new_project import _NONE_PROFILE_VALUE, NewProjectModal
from epublate.app.screens.open_project import OpenProjectModal
from epublate.app.screens.projects import ProjectsScreen
from epublate.core.project import Project
from epublate.core.style import (
    DEFAULT_STYLE_PROFILE,
    PROFILE_REGISTRY,
    resolve_style_guide,
)


@pytest.mark.asyncio
async def test_new_project_modal_creates_and_opens_dashboard(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    target_dir = tmp_path / "fresh"
    recents = tmp_path / "recents.json"

    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, NewProjectModal)

        from textual.widgets import Input

        modal.query_one("#new-project-source", Input).value = str(sample_epub_path)
        modal.query_one("#new-project-target-lang", Input).value = "pt"
        modal.query_one("#new-project-source-lang", Input).value = "en"
        modal.query_one("#new-project-out", Input).value = str(target_dir)
        modal.query_one("#new-project-name", Input).value = "Sample"

        await pilot.press("ctrl+s")
        await pilot.pause()

        # On success the modal dismisses and the Dashboard is pushed.
        for _ in range(20):
            if isinstance(pilot.app.screen, DashboardScreen):
                break
            await pilot.pause()
        assert isinstance(pilot.app.screen, DashboardScreen)

        # The recents store should now have the project at the top.
        loaded = RecentsStore.load(recents)
        assert loaded.entries
        assert Path(loaded.entries[0].project_dir).resolve() == target_dir.resolve()
        assert loaded.entries[0].source_lang == "en"
        assert loaded.entries[0].target_lang == "pt"

        # Pop back to the Projects screen and quit cleanly.
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ProjectsScreen)
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_project_modal_surfaces_validation_errors(tmp_path: Path) -> None:
    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, NewProjectModal)
        await pilot.press("ctrl+s")
        await pilot.pause()
        from textual.widgets import Static

        err = str(modal.query_one("#new-project-error", Static).render())
        assert "Source ePub" in err
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ProjectsScreen)
        await pilot.press("q")


@pytest.mark.asyncio
async def test_open_modal_opens_existing_project(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    project_dir = tmp_path / "demo"
    project = Project.create(
        sample_epub_path,
        out_dir=project_dir,
        source_lang="en",
        target_lang="pt",
        name="Demo",
    )
    project.close()

    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("o")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, OpenProjectModal)
        from textual.widgets import Input

        modal.query_one("#open-project-path", Input).value = str(project_dir)
        await pilot.press("ctrl+s")
        await pilot.pause()
        for _ in range(20):
            if isinstance(pilot.app.screen, DashboardScreen):
                break
            await pilot.pause()
        assert isinstance(pilot.app.screen, DashboardScreen)
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ProjectsScreen)
        await pilot.press("q")


@pytest.mark.asyncio
async def test_open_modal_rejects_non_directory(tmp_path: Path) -> None:
    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("o")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, OpenProjectModal)
        from textual.widgets import Input, Static

        modal.query_one("#open-project-path", Input).value = str(
            tmp_path / "definitely-not-here"
        )
        await pilot.press("ctrl+s")
        await pilot.pause()
        err = str(modal.query_one("#open-project-error", Static).render())
        assert "Not a directory" in err
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ProjectsScreen)
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_project_modal_defaults_to_projects_root(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    """Leaving ``Output dir`` blank lands the project under the configured root."""

    projects_root = tmp_path / "my-projects-root"
    projects_root.mkdir()
    recents = tmp_path / "recents.json"
    app = EpublateApp(
        initial_screen=ProjectsScreen(recents_path=recents),
    )
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        from textual.widgets import Input

        modal = pilot.app.screen
        assert isinstance(modal, NewProjectModal)
        # Swap in a modal that points at the test-owned projects root so
        # the default resolution is deterministic.
        modal._projects_root = projects_root
        modal.query_one("#new-project-source", Input).value = str(sample_epub_path)
        modal.query_one("#new-project-target-lang", Input).value = "pt"
        modal.query_one("#new-project-source-lang", Input).value = "en"
        await pilot.press("ctrl+s")
        await pilot.pause()
        for _ in range(20):
            if isinstance(pilot.app.screen, DashboardScreen):
                break
            await pilot.pause()
        assert isinstance(pilot.app.screen, DashboardScreen)

        loaded = RecentsStore.load(recents)
        assert loaded.entries
        latest_dir = Path(loaded.entries[0].project_dir).resolve()
        assert latest_dir.parent == projects_root.resolve()
        assert latest_dir.name == sample_epub_path.stem
        await pilot.press("q")
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_project_modal_autodedups_existing_folder(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    """If <root>/<stem> already has files, fall back to <stem>-2."""

    projects_root = tmp_path / "root"
    projects_root.mkdir()
    occupied = projects_root / sample_epub_path.stem
    occupied.mkdir()
    (occupied / "existing.txt").write_text("leave me alone")

    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        from textual.widgets import Input

        modal = pilot.app.screen
        assert isinstance(modal, NewProjectModal)
        modal._projects_root = projects_root
        modal.query_one("#new-project-source", Input).value = str(sample_epub_path)
        modal.query_one("#new-project-target-lang", Input).value = "pt"
        modal.query_one("#new-project-source-lang", Input).value = "en"
        await pilot.press("ctrl+s")
        await pilot.pause()
        for _ in range(20):
            if isinstance(pilot.app.screen, DashboardScreen):
                break
            await pilot.pause()
        assert isinstance(pilot.app.screen, DashboardScreen)

        loaded = RecentsStore.load(recents)
        latest = Path(loaded.entries[0].project_dir).resolve()
        assert latest == (projects_root / f"{sample_epub_path.stem}-2").resolve()
        # The existing folder must be untouched.
        assert (occupied / "existing.txt").read_text() == "leave me alone"
        await pilot.press("q")
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_project_modal_source_browse_opens_file_picker(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    """Ctrl+O on the New Project modal surfaces the FileBrowserModal."""

    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, NewProjectModal)
        # Seed the source input so the browser can jump to its parent.
        from textual.widgets import Input

        modal.query_one("#new-project-source", Input).value = str(sample_epub_path)
        await pilot.press("ctrl+o")
        await pilot.pause()
        browser = pilot.app.screen
        assert isinstance(browser, FileBrowserModal)
        # Cancel and return to the New Project modal.
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, NewProjectModal)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_open_project_modal_browse_opens_directory_picker(
    tmp_path: Path,
) -> None:
    """Ctrl+O on the Open modal surfaces FileBrowserModal in directory mode."""

    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("o")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, OpenProjectModal)
        await pilot.press("ctrl+o")
        await pilot.pause()
        browser = pilot.app.screen
        assert isinstance(browser, FileBrowserModal)
        # The directory-mode browser exposes a "Pick this folder" button.
        from textual.widgets import Button

        browser.query_one("#fb-pick-here", Button)
        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, OpenProjectModal)
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_project_modal_defaults_tone_to_literary_fiction(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    """The tone Select pre-populates with the default preset (PRD F-STYLE-1).

    The TextArea must be seeded with the preset's prompt block so the
    curator can read what the LLM will see *before* hitting Create.
    """

    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, NewProjectModal)
        from textual.widgets import Select, TextArea

        select = modal.query_one("#new-project-tone", Select)
        assert select.value == DEFAULT_STYLE_PROFILE

        text_area = modal.query_one("#new-project-tone-text", TextArea)
        assert (
            text_area.text.strip()
            == PROFILE_REGISTRY[DEFAULT_STYLE_PROFILE].prompt_block.strip()
        )

        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_project_modal_swap_preset_reseeds_textarea(
    tmp_path: Path,
) -> None:
    """Selecting a different preset replaces the TextArea contents.

    Curators are allowed to tweak the prose, but they should not be
    surprised when the visible text and the chosen preset disagree —
    swapping the preset deliberately overwrites the TextArea.
    """

    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, NewProjectModal)
        from textual.widgets import Select, TextArea

        select = modal.query_one("#new-project-tone", Select)
        select.value = "children_picture"
        await pilot.pause()
        text_area = modal.query_one("#new-project-tone-text", TextArea)
        assert (
            text_area.text.strip()
            == PROFILE_REGISTRY["children_picture"].prompt_block.strip()
        )

        select.value = _NONE_PROFILE_VALUE
        await pilot.pause()
        assert text_area.disabled is True
        assert text_area.text == ""

        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_project_modal_persists_chosen_tone(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    """Choosing a non-default preset persists in the project row + DB.

    Verifies the end-to-end wiring: Select.value → Project.create →
    SQLite project.style_profile + style_guide. The translator pipeline
    resolves the prompt by reading these columns.
    """

    target_dir = tmp_path / "tone-default"
    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, NewProjectModal)
        from textual.widgets import Input, Select

        modal.query_one("#new-project-source", Input).value = str(sample_epub_path)
        modal.query_one("#new-project-target-lang", Input).value = "pt"
        modal.query_one("#new-project-source-lang", Input).value = "en"
        modal.query_one("#new-project-out", Input).value = str(target_dir)
        modal.query_one("#new-project-name", Input).value = "Tone"
        modal.query_one("#new-project-tone", Select).value = "explicit_adult"
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        for _ in range(20):
            if isinstance(pilot.app.screen, DashboardScreen):
                break
            await pilot.pause()
        assert isinstance(pilot.app.screen, DashboardScreen)

        project = Project.open(target_dir)
        try:
            assert project.style_profile == "explicit_adult"
            expected = resolve_style_guide(profile_id="explicit_adult")
            assert project.style_guide == expected
        finally:
            project.close()
        await pilot.press("q")
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_project_modal_persists_custom_tone_text(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    """Editing the tone TextArea stores the curator's prose verbatim.

    Important for PRD F-STYLE-1: a custom paragraph must round-trip
    unchanged so the LLM sees exactly what the curator typed (and the
    cache key reflects it).
    """

    target_dir = tmp_path / "tone-custom"
    recents = tmp_path / "recents.json"
    custom = "Translate as a courtroom drama transcript: terse, factual, no flourishes."
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, NewProjectModal)
        from textual.widgets import Input, TextArea

        modal.query_one("#new-project-source", Input).value = str(sample_epub_path)
        modal.query_one("#new-project-target-lang", Input).value = "pt"
        modal.query_one("#new-project-source-lang", Input).value = "en"
        modal.query_one("#new-project-out", Input).value = str(target_dir)
        modal.query_one("#new-project-name", Input).value = "Tone"
        modal.query_one("#new-project-tone-text", TextArea).text = custom
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        for _ in range(20):
            if isinstance(pilot.app.screen, DashboardScreen):
                break
            await pilot.pause()
        assert isinstance(pilot.app.screen, DashboardScreen)

        project = Project.open(target_dir)
        try:
            assert project.style_profile == DEFAULT_STYLE_PROFILE
            assert project.style_guide is not None
            assert custom in project.style_guide
        finally:
            project.close()
        await pilot.press("q")
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_project_modal_none_tone_clears_style_guide(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    """Selecting 'None' yields a project with no style guide at all."""

    target_dir = tmp_path / "tone-none"
    recents = tmp_path / "recents.json"
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, NewProjectModal)
        from textual.widgets import Input, Select

        modal.query_one("#new-project-source", Input).value = str(sample_epub_path)
        modal.query_one("#new-project-target-lang", Input).value = "pt"
        modal.query_one("#new-project-source-lang", Input).value = "en"
        modal.query_one("#new-project-out", Input).value = str(target_dir)
        modal.query_one("#new-project-name", Input).value = "Tone"
        modal.query_one("#new-project-tone", Select).value = _NONE_PROFILE_VALUE
        await pilot.pause()
        await pilot.press("ctrl+s")
        await pilot.pause()
        for _ in range(20):
            if isinstance(pilot.app.screen, DashboardScreen):
                break
            await pilot.pause()
        assert isinstance(pilot.app.screen, DashboardScreen)

        project = Project.open(target_dir)
        try:
            assert project.style_profile is None
            assert project.style_guide is None
        finally:
            project.close()
        await pilot.press("q")
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_project_modal_auto_sniff_preselects_tone(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    """When auto-sniff is on and a source is picked, the helper's
    suggested preset replaces the default selection (PRD F-STYLE-4)."""

    import json

    from epublate.llm.mock import MockLLMProvider

    provider = MockLLMProvider()
    provider.set_response(
        json.dumps(
            {
                "entities": [],
                "pov": "",
                "tense": "",
                "register": "playful",
                "audience": "children",
                "notes": "",
            }
        )
    )

    recents = tmp_path / "recents.json"
    modal = NewProjectModal(
        recents_path=recents,
        auto_tone_sniff=True,
        tone_provider_factory=lambda: provider,
        tone_helper_model="helper-model",
    )
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.app.push_screen(modal)
        await pilot.pause()
        from textual.widgets import Input, Select, Static

        # Trigger the source-pick code path that the file browser would.
        modal._on_source_picked(sample_epub_path)
        await pilot.pause()
        # Wait for the worker thread + posted message to round-trip.
        for _ in range(40):
            if not modal._sniff_in_flight:
                break
            await pilot.pause()
        assert provider.call_count == 1
        select = modal.query_one("#new-project-tone", Select)
        assert select.value == "children_picture"
        status = str(modal.query_one("#new-project-tone-status", Static).render())
        assert "Children" in status
        # Re-picking the same source path must NOT refire the helper.
        modal._on_source_picked(sample_epub_path)
        await pilot.pause()
        assert provider.call_count == 1
        # Sanity: source input was populated.
        assert modal.query_one("#new-project-source", Input).value == str(
            sample_epub_path
        )

        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_project_modal_auto_sniff_respects_manual_pick(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    """If the curator picked a tone before the sniff settles, keep their
    pick. The status row still surfaces the helper's suggestion."""

    import json

    from epublate.llm.mock import MockLLMProvider

    provider = MockLLMProvider()
    provider.set_response(
        json.dumps(
            {
                "entities": [],
                "pov": "",
                "tense": "",
                "register": "playful",
                "audience": "children",
                "notes": "",
            }
        )
    )

    recents = tmp_path / "recents.json"
    modal = NewProjectModal(
        recents_path=recents,
        auto_tone_sniff=True,
        tone_provider_factory=lambda: provider,
        tone_helper_model="helper-model",
    )
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.app.push_screen(modal)
        await pilot.pause()
        from textual.widgets import Select, Static

        # Curator manually overrides BEFORE the sniff fires.
        select = modal.query_one("#new-project-tone", Select)
        select.value = "explicit_adult"
        await pilot.pause()
        assert modal._tone_was_user_set is True

        modal._on_source_picked(sample_epub_path)
        for _ in range(40):
            if not modal._sniff_in_flight:
                break
            await pilot.pause()
        # Manual pick must survive the suggestion.
        assert select.value == "explicit_adult"
        status = str(modal.query_one("#new-project-tone-status", Static).render())
        assert "manual pick" in status

        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_project_modal_auto_sniff_disabled_when_factory_missing(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    """Without a provider factory, the modal must not crash and must
    not touch the tone status row — the sniff stays dormant."""

    recents = tmp_path / "recents.json"
    modal = NewProjectModal(
        recents_path=recents,
        auto_tone_sniff=True,
        tone_provider_factory=None,
        tone_helper_model=None,
    )
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.app.push_screen(modal)
        await pilot.pause()
        modal._on_source_picked(sample_epub_path)
        await pilot.pause()
        from textual.widgets import Static

        status = str(modal.query_one("#new-project-tone-status", Static).render())
        assert status == ""
        assert modal._sniff_in_flight is False

        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_new_project_modal_auto_sniff_failure_shows_quiet_status(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    """A helper that returns garbage produces a dim italic status —
    the modal stays usable, the curator can still pick a tone manually
    or hit Create."""

    from epublate.llm.mock import MockLLMProvider

    provider = MockLLMProvider()
    provider.set_response("not even close to JSON")

    recents = tmp_path / "recents.json"
    modal = NewProjectModal(
        recents_path=recents,
        auto_tone_sniff=True,
        tone_provider_factory=lambda: provider,
        tone_helper_model="helper-model",
    )
    app = EpublateApp(initial_screen=ProjectsScreen(recents_path=recents))
    async with app.run_test() as pilot:
        await pilot.app.push_screen(modal)
        await pilot.pause()
        modal._on_source_picked(sample_epub_path)
        for _ in range(40):
            if not modal._sniff_in_flight:
                break
            await pilot.pause()

        from textual.widgets import Select, Static

        # Default tone is unchanged.
        select = modal.query_one("#new-project-tone", Select)
        assert select.value == DEFAULT_STYLE_PROFILE
        status = str(modal.query_one("#new-project-tone-status", Static).render())
        assert "skipped" in status

        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("q")


@pytest.mark.asyncio
async def test_enter_on_recent_row_opens_dashboard(
    tmp_path: Path, sample_epub_path: Path
) -> None:
    project_dir = tmp_path / "demo"
    project = Project.create(
        sample_epub_path,
        out_dir=project_dir,
        source_lang="en",
        target_lang="pt",
        name="Demo",
    )
    project.close()

    recents = tmp_path / "recents.json"
    store = RecentsStore()
    store.upsert(
        RecentProject(
            project_dir=str(project_dir),
            name="Demo",
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
        await pilot.press("enter")
        await pilot.pause()
        for _ in range(20):
            if isinstance(pilot.app.screen, DashboardScreen):
                break
            await pilot.pause()
        assert isinstance(pilot.app.screen, DashboardScreen)
        await pilot.press("q")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ProjectsScreen)
        await pilot.press("q")
