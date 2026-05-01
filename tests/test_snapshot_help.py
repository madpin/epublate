"""Snapshot baseline for the Help / cheat-sheet modal (PRD §4.6 / M6)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from epublate.app.main import EpublateApp
from epublate.app.screens.dashboard import DashboardScreen
from epublate.core.project import Project
from epublate.llm.mock import MockLLMProvider

TERMINAL_SIZE: tuple[int, int] = (120, 36)


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(chapters=[("Chapter One", "<p>Hello.</p>")])
    return Project.create(
        src,
        out_dir=tmp_path / "snapshot-proj",
        source_lang="en",
        target_lang="pt",
    )


def test_snapshot_help(
    snap_compare: Callable[..., bool],
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(project, provider_factory=MockLLMProvider)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        assert snap_compare(app, press=["?"], terminal_size=TERMINAL_SIZE)
    finally:
        project.close()
