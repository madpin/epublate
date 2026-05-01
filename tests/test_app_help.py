"""Tests for the help / cheat-sheet modal (PRD §4.6 / M6)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.main import EpublateApp
from epublate.app.screens.dashboard import DashboardScreen
from epublate.app.screens.help import HelpScreen, _collect_bindings
from epublate.app.screens.projects import ProjectsScreen
from epublate.core.project import Project
from epublate.llm.mock import MockLLMProvider


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(chapters=[("Solo", "<p>Hello.</p>")])
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def test_collect_bindings_dedupes_and_includes_hidden() -> None:
    rows = _collect_bindings(ProjectsScreen())
    keys = {r[0] for r in rows}
    assert "n" in keys
    assert "o" in keys


@pytest.mark.asyncio
async def test_help_modal_opens_on_question_mark(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(
            project,
            provider_factory=MockLLMProvider,
        )
        config_path = tmp_path / "ui.toml"
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("?")
            await pilot.pause()
            assert isinstance(pilot.app.screen, HelpScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(pilot.app.screen, DashboardScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_help_modal_opens_on_f1(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(
            project,
            provider_factory=MockLLMProvider,
        )
        config_path = tmp_path / "ui.toml"
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("f1")
            await pilot.pause()
            assert isinstance(pilot.app.screen, HelpScreen)
    finally:
        project.close()
