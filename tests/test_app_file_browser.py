"""End-to-end tests for :class:`FileBrowserModal` (PRD §4.6)."""

from __future__ import annotations

from pathlib import Path

import pytest
from textual.widgets import Button, DataTable, Input

from epublate.app.main import EpublateApp
from epublate.app.screens.file_browser import FileBrowserModal
from epublate.app.screens.projects import ProjectsScreen


async def _push_browser(app: EpublateApp, **kwargs: object) -> None:
    # ``push_screen`` without a callback is still valid for tests that
    # just want to drive the modal and inspect its state.
    app.push_screen(FileBrowserModal(**kwargs))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_file_browser_lists_epub_and_directories(tmp_path: Path) -> None:
    # Use an isolated subdir because conftest drops XDG dirs in ``tmp_path``.
    browse_root = tmp_path / "browse"
    browse_root.mkdir()
    (browse_root / "sub").mkdir()
    (browse_root / "book.epub").write_bytes(b"PK\x03\x04")
    (browse_root / "notes.txt").write_text("ignore me")

    app = EpublateApp(initial_screen=ProjectsScreen())
    async with app.run_test() as pilot:
        await _push_browser(pilot.app, mode="epub", start_path=browse_root)
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, FileBrowserModal)
        table = modal.query_one("#fb-table", DataTable)
        # parent row + one dir + one epub; the .txt file is filtered out.
        assert table.row_count == 3
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_file_browser_directory_mode_hides_files(tmp_path: Path) -> None:
    browse_root = tmp_path / "browse"
    browse_root.mkdir()
    (browse_root / "proj").mkdir()
    (browse_root / "README.md").write_text("hi")
    (browse_root / "a.epub").write_bytes(b"PK")

    app = EpublateApp(initial_screen=ProjectsScreen())
    async with app.run_test() as pilot:
        await _push_browser(pilot.app, mode="directory", start_path=browse_root)
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, FileBrowserModal)
        table = modal.query_one("#fb-table", DataTable)
        # parent + proj/ only; files are hidden in directory mode.
        assert table.row_count == 2
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_file_browser_enter_descends_then_picks_epub(tmp_path: Path) -> None:
    browse_root = tmp_path / "browse"
    browse_root.mkdir()
    sub = browse_root / "subdir"
    sub.mkdir()
    target = sub / "book.epub"
    target.write_bytes(b"PK\x03\x04")
    picked: list[Path | None] = []

    def _capture(result: Path | None) -> None:
        picked.append(result)

    app = EpublateApp(initial_screen=ProjectsScreen())
    async with app.run_test() as pilot:
        pilot.app.push_screen(
            FileBrowserModal(mode="epub", start_path=browse_root), _capture
        )
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, FileBrowserModal)
        # Row 0 is ``..``; move to row 1 (``subdir``) and press Enter.
        table = modal.query_one("#fb-table", DataTable)
        table.move_cursor(row=1)
        await pilot.press("enter")
        await pilot.pause()
        table = modal.query_one("#fb-table", DataTable)
        # Now inside subdir: parent + book.epub.
        assert table.row_count == 2
        table.move_cursor(row=1)
        await pilot.press("enter")
        await pilot.pause()

    assert picked == [target.resolve()]


@pytest.mark.asyncio
async def test_file_browser_directory_mode_pick_this_folder(tmp_path: Path) -> None:
    picked: list[Path | None] = []

    def _capture(result: Path | None) -> None:
        picked.append(result)

    app = EpublateApp(initial_screen=ProjectsScreen())
    async with app.run_test() as pilot:
        pilot.app.push_screen(
            FileBrowserModal(mode="directory", start_path=tmp_path), _capture
        )
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, FileBrowserModal)
        # Click the 'Pick this folder' button.
        button = modal.query_one("#fb-pick-here", Button)
        button.press()
        await pilot.pause()
    assert picked == [tmp_path.resolve()]


@pytest.mark.asyncio
async def test_file_browser_path_input_jumps_directly(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "inner"
    target.mkdir(parents=True)

    app = EpublateApp(initial_screen=ProjectsScreen())
    async with app.run_test() as pilot:
        await _push_browser(pilot.app, mode="directory", start_path=tmp_path)
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, FileBrowserModal)
        path_input = modal.query_one("#fb-path-input", Input)
        path_input.value = str(target)
        path_input.focus()
        await pilot.press("enter")
        await pilot.pause()
        assert modal._current == target.resolve()
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_file_browser_backspace_goes_up(tmp_path: Path) -> None:
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)

    app = EpublateApp(initial_screen=ProjectsScreen())
    async with app.run_test() as pilot:
        await _push_browser(pilot.app, mode="directory", start_path=nested)
        await pilot.pause()
        modal = pilot.app.screen
        assert isinstance(modal, FileBrowserModal)
        await pilot.press("backspace")
        await pilot.pause()
        assert modal._current == nested.parent.resolve()
        await pilot.press("escape")


@pytest.mark.asyncio
async def test_file_browser_cancel_returns_none(tmp_path: Path) -> None:
    result: list[Path | None] = []

    def _capture(r: Path | None) -> None:
        result.append(r)

    app = EpublateApp(initial_screen=ProjectsScreen())
    async with app.run_test() as pilot:
        pilot.app.push_screen(
            FileBrowserModal(mode="epub", start_path=tmp_path), _capture
        )
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert result == [None]
