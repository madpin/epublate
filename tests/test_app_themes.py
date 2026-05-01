"""Tests for theming + persisted UI preferences (PRD §4.6 / M6)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.config import UIConfig
from epublate.app.main import EpublateApp
from epublate.app.screens.dashboard import DashboardScreen
from epublate.app.themes import (
    EPUBLATE_CONTRAST_THEME_NAME,
    EPUBLATE_THEME_ORDER,
    epublate_contrast_theme,
)
from epublate.core.project import Project
from epublate.llm.mock import MockLLMProvider


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(chapters=[("Solo", "<p>Hello.</p>")])
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def test_contrast_theme_is_dark_with_high_contrast_palette() -> None:
    theme = epublate_contrast_theme()
    assert theme.name == EPUBLATE_CONTRAST_THEME_NAME
    assert theme.dark is True
    assert theme.background == "#000000"
    assert theme.foreground == "#FFFFFF"


def test_theme_order_includes_contrast_variant() -> None:
    assert EPUBLATE_CONTRAST_THEME_NAME in EPUBLATE_THEME_ORDER
    # The branded ``epublate`` theme is the first entry — fresh installs
    # land on the polished look and cycle from there.
    assert EPUBLATE_THEME_ORDER[0] == "epublate"
    assert "textual-dark" in EPUBLATE_THEME_ORDER
    assert "textual-light" in EPUBLATE_THEME_ORDER


@pytest.mark.asyncio
async def test_cycle_theme_advances_through_order_and_persists(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(project, provider_factory=MockLLMProvider)
        config_path = tmp_path / "ui.toml"
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert pilot.app.theme == EPUBLATE_THEME_ORDER[0]
            for i in range(1, len(EPUBLATE_THEME_ORDER)):
                await pilot.press("T")
                await pilot.pause()
                assert pilot.app.theme == EPUBLATE_THEME_ORDER[i]
            await pilot.press("T")
            await pilot.pause()
            assert pilot.app.theme == EPUBLATE_THEME_ORDER[0]

        reloaded = UIConfig.load(config_path)
        assert reloaded.theme == EPUBLATE_THEME_ORDER[0]
    finally:
        project.close()


@pytest.mark.asyncio
async def test_saved_theme_is_restored_on_next_launch(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        config_path = tmp_path / "ui.toml"
        UIConfig(theme=EPUBLATE_CONTRAST_THEME_NAME).save(config_path)

        screen = DashboardScreen(project, provider_factory=MockLLMProvider)
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert pilot.app.theme == EPUBLATE_CONTRAST_THEME_NAME
    finally:
        project.close()


@pytest.mark.asyncio
async def test_unknown_saved_theme_falls_back_to_default(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        config_path = tmp_path / "ui.toml"
        UIConfig(theme="nonexistent-theme").save(config_path)

        screen = DashboardScreen(project, provider_factory=MockLLMProvider)
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            # Falls through to whatever Textual's default is.
            assert pilot.app.theme in pilot.app.available_themes
    finally:
        project.close()
