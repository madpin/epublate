"""Pilot tests for the Glossary screen (PRD sections 4.6 / 7.4 / 7.5 / M3)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.main import EpublateApp
from epublate.app.screens.glossary import GlossaryScreen
from epublate.core.project import Project
from epublate.db import repo


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1><p>Élise smiled at Hugo.</p><p>Hugo nodded back.</p>",
            )
        ]
    )
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def _seed_one_entry(project: Project) -> str:
    entry = repo.create_glossary_entry(
        project.engine,
        project_id=project.project_id,
        source_term="Élise",
        target_term="Elisa",
        type="character",
        status="confirmed",
        source_aliases=["Lise"],
    )
    return entry.id


@pytest.mark.asyncio
async def test_glossary_screen_renders_entries(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_one_entry(project)
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, GlossaryScreen)
            assert len(current.entries) == 1
            assert current.entries[0].source_term == "Élise"
    finally:
        project.close()


@pytest.mark.asyncio
async def test_glossary_lock_keystroke_promotes_entry(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        entry_id = _seed_one_entry(project)
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            entry = repo.get_glossary_entry(project.engine, entry_id)
            assert entry is not None
            assert entry.status == "locked"
            revisions = repo.list_glossary_revisions(project.engine, entry_id)
            assert len(revisions) == 1
    finally:
        project.close()


@pytest.mark.asyncio
async def test_glossary_filter_cycles_through_statuses(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Élise",
            target_term="Elisa",
            status="locked",
        )
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Hugo",
            target_term="Hugo",
            status="proposed",
        )
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, GlossaryScreen)
            assert current.filter_status == "all"
            await pilot.press("f")
            await pilot.pause()
            assert current.filter_status == "proposed"
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_g_binding_pushes_glossary_screen(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    from epublate.app.screens.reader import ReaderScreen
    from epublate.llm.mock import MockLLMProvider

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("g")
            await pilot.pause()
            assert isinstance(pilot.app.screen, GlossaryScreen)
            # Pop programmatically — the global app-level ``q`` binding is
            # ``priority=True`` (always quits) so screen-level ``q`` can't
            # pop in tests; the assertion that matters is that the screen
            # was pushed.
            pilot.app.pop_screen()
            await pilot.pause()
            assert isinstance(pilot.app.screen, ReaderScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_glossary_cascade_no_op_when_no_segments_match(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Cascade with no affected segments should short-circuit (no modal)."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        # Create an entry whose source/target won't appear anywhere.
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Nonexistent",
            target_term="Inexistente",
            status="confirmed",
        )
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            # Still on the GlossaryScreen — cascade was a no-op.
            assert isinstance(pilot.app.screen, GlossaryScreen)
    finally:
        project.close()
