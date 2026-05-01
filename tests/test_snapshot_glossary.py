"""Snapshot baseline for the Glossary screen (PRD §4.6 / M6)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from epublate.app.main import EpublateApp
from epublate.app.screens.glossary import GlossaryScreen
from epublate.core.project import Project
from epublate.db import repo

TERMINAL_SIZE: tuple[int, int] = (120, 36)


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(chapters=[("Chapter One", "<p>Hello, brave hero.</p>")])
    project = Project.create(
        src,
        out_dir=tmp_path / "snapshot-proj",
        source_lang="en",
        target_lang="pt",
    )
    repo.create_glossary_entry(
        project.engine,
        project_id=project.project_id,
        type="character",
        source_term="Hero",
        target_term="Heroína",
        status="locked",
        notes="Confirmed by curator.",
    )
    repo.create_glossary_entry(
        project.engine,
        project_id=project.project_id,
        type="place",
        source_term="Marenglade",
        target_term="Marenglade",
        status="proposed",
    )
    return project


def test_snapshot_glossary(
    snap_compare: Callable[..., bool],
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        assert snap_compare(app, terminal_size=TERMINAL_SIZE)
    finally:
        project.close()
