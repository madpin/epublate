"""Snapshot baseline for the Project Dashboard (PRD §4.6 / M6)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from epublate.app.main import EpublateApp
from epublate.app.screens.dashboard import DashboardScreen
from epublate.core.project import Project
from epublate.llm.mock import MockLLMProvider
from tests._snapshot_helpers import mask_dashboard_project

TERMINAL_SIZE: tuple[int, int] = (120, 36)


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(
        chapters=[
            (
                "Chapter One",
                "<h1>Chapter One</h1>"
                "<p>The brave hero sailed for the lost citadel.</p>"
                "<p>Behind them, the city of Marenglade slept.</p>",
            )
        ]
    )
    return Project.create(
        src,
        out_dir=tmp_path / "snapshot-proj",
        source_lang="en",
        target_lang="pt",
    )


def test_snapshot_dashboard(
    snap_compare: Callable[..., bool],
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(
            project,
            provider_factory=MockLLMProvider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        assert snap_compare(
            app,
            terminal_size=TERMINAL_SIZE,
            run_before=mask_dashboard_project,
        )
    finally:
        project.close()
