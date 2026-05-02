"""Pilot tests for ``LoreBookDashboardScreen`` import-from-project flow.

PRD F-LB-10 phase 3: the curator can pull a project's curated glossary
into an open Lore Book and resolve conflicts interactively. We don't
test the full per-conflict modal walk through the keyboard here (that
requires async UI plumbing the existing tests don't already exercise);
the more important guarantees are:

* the dashboard can mount,
* ``action_import_project`` pushes :class:`LoreImportProjectModal`,
* the conflict-walking helpers update the dashboard summary correctly
  end-to-end when driven directly with a synthetic conflict list.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.main import EpublateApp
from epublate.app.screens.lore_book_dashboard import (
    ConflictDecision,
    ImportProjectRequest,
    LoreBookDashboardScreen,
    LoreImportProjectModal,
)
from epublate.core.project import Project
from epublate.db import repo
from epublate.db.schema import GlossaryStatus
from epublate.lore import LoreBook


def _make_lore_book(tmp_path: Path) -> LoreBook:
    return LoreBook.create(
        out_dir=tmp_path / "lore.epublate-lore",
        name="Series Lore",
        source_lang="en",
        target_lang="pt",
    )


def _make_project_with_geralt(
    tiny_factory: Callable[..., Path],
    tmp_path: Path,
    *,
    target_term: str,
) -> Path:
    src = tiny_factory(
        chapters=[("Chap", "<h1>Chap</h1><p>Hello.</p>")],
    )
    project = Project.create(
        src,
        out_dir=tmp_path / "proj",
        source_lang="en",
        target_lang="pt",
    )
    try:
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Geralt",
            target_term=target_term,
            type="character",
            status=GlossaryStatus.CONFIRMED,
        )
        return project.project_dir
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_mounts_with_import_binding(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    book = _make_lore_book(tmp_path)
    try:
        screen = LoreBookDashboardScreen(book)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, LoreBookDashboardScreen)
            actions = {b.action for b in current.BINDINGS}  # type: ignore[union-attr]
            assert "import_project" in actions
    finally:
        book.close()


@pytest.mark.asyncio
async def test_action_import_project_pushes_modal(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    book = _make_lore_book(tmp_path)
    try:
        screen = LoreBookDashboardScreen(book)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("p")
            await pilot.pause()
            assert isinstance(pilot.app.screen, LoreImportProjectModal)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(pilot.app.screen, LoreBookDashboardScreen)
    finally:
        book.close()


@pytest.mark.asyncio
async def test_no_conflict_path_writes_status_directly(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project_dir = _make_project_with_geralt(
        tiny_epub_factory, tmp_path, target_term="Geralt de Rívia"
    )
    book = _make_lore_book(tmp_path)
    try:
        screen = LoreBookDashboardScreen(book)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, LoreBookDashboardScreen)
            current._on_import_project_chosen(
                ImportProjectRequest(project_dir=project_dir, policy="collect")
            )
            await pilot.pause()
            from textual.widgets import Static

            status = current.query_one("#lore-dashboard-status", Static)
            text = str(status.render())
            assert "Imported from" in text
            assert "1 created" in text
            entries = repo.list_glossary_entries(book.engine, book.project_id)
            assert any(e.target_term == "Geralt de Rívia" for e in entries)
    finally:
        book.close()


@pytest.mark.asyncio
async def test_conflict_walk_use_incoming_overwrites_destination(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The interactive flow must apply ``use_incoming`` decisions to the DB."""
    project_dir = _make_project_with_geralt(
        tiny_epub_factory, tmp_path, target_term="Geralt the Witcher"
    )
    book = _make_lore_book(tmp_path)
    try:
        repo.create_glossary_entry(
            book.engine,
            project_id=book.project_id,
            source_term="Geralt",
            target_term="Geralt de Rívia",
            type="character",
            status=GlossaryStatus.LOCKED,
        )

        screen = LoreBookDashboardScreen(book)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, LoreBookDashboardScreen)
            current._on_import_project_chosen(
                ImportProjectRequest(project_dir=project_dir, policy="collect")
            )
            await pilot.pause()
            # Modal pushed for the single conflict. Drive the decision
            # via the action callback path so we don't depend on the
            # screen pop semantics in the pilot harness.
            current._on_conflict_decision(ConflictDecision(action="use_incoming"))
            await pilot.pause()

        existing = repo.find_glossary_entry_by_source_term(
            book.engine,
            project_id=book.project_id,
            source_term="Geralt",
            type="character",
        )
        assert existing is not None
        assert existing.target_term == "Geralt the Witcher"
    finally:
        book.close()


@pytest.mark.asyncio
async def test_conflict_walk_keep_existing_preserves_destination(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project_dir = _make_project_with_geralt(
        tiny_epub_factory, tmp_path, target_term="Geralt the Witcher"
    )
    book = _make_lore_book(tmp_path)
    try:
        repo.create_glossary_entry(
            book.engine,
            project_id=book.project_id,
            source_term="Geralt",
            target_term="Geralt de Rívia",
            type="character",
            status=GlossaryStatus.LOCKED,
        )
        screen = LoreBookDashboardScreen(book)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, LoreBookDashboardScreen)
            current._on_import_project_chosen(
                ImportProjectRequest(project_dir=project_dir, policy="collect")
            )
            await pilot.pause()
            current._on_conflict_decision(ConflictDecision(action="keep_existing"))
            await pilot.pause()

        existing = repo.find_glossary_entry_by_source_term(
            book.engine,
            project_id=book.project_id,
            source_term="Geralt",
            type="character",
        )
        assert existing is not None
        assert existing.target_term == "Geralt de Rívia"
    finally:
        book.close()


@pytest.mark.asyncio
async def test_conflict_walk_cancel_aborts_remaining(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Aborting must surface in the status line as 'unresolved'."""
    project_dir = _make_project_with_geralt(
        tiny_epub_factory, tmp_path, target_term="Geralt the Witcher"
    )
    book = _make_lore_book(tmp_path)
    try:
        repo.create_glossary_entry(
            book.engine,
            project_id=book.project_id,
            source_term="Geralt",
            target_term="Geralt de Rívia",
            type="character",
            status=GlossaryStatus.LOCKED,
        )
        screen = LoreBookDashboardScreen(book)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, LoreBookDashboardScreen)
            current._on_import_project_chosen(
                ImportProjectRequest(project_dir=project_dir, policy="collect")
            )
            await pilot.pause()
            current._on_conflict_decision(ConflictDecision(action="cancel"))
            await pilot.pause()
            from textual.widgets import Static

            status = current.query_one("#lore-dashboard-status", Static)
            assert "aborted" in str(status.render())
    finally:
        book.close()
