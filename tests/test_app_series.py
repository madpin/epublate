"""Tests for the series rollup view (PRD §4.6 / F-LB-10)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.recents import RecentProject, RecentsStore
from epublate.app.screens.series import SeriesScreen
from epublate.core.project import Project
from epublate.db import repo
from epublate.db.schema import AttachedLoreMode
from epublate.lore import LoreBook


def _make_lore_book(tmp_path: Path, *, name: str = "Series Lore") -> LoreBook:
    return LoreBook.create(
        out_dir=tmp_path / "lore",
        name=name,
        source_lang="en",
        target_lang="pt",
    )


def _make_project_attached(
    tiny_factory: Callable[..., Path],
    tmp_path: Path,
    lore: LoreBook,
    *,
    project_name: str,
    mode: str = AttachedLoreMode.READ_ONLY,
) -> Project:
    src = tiny_factory(
        chapters=[
            (
                "Chap",
                "<h1>Chap</h1><p>Hello.</p>",
            )
        ]
    )
    project = Project.create(
        src,
        out_dir=tmp_path / project_name,
        source_lang="en",
        target_lang="pt",
    )
    repo.attach_lore_book(
        project.engine,
        project_id=project.project_id,
        lore_path=str(lore.lore_dir),
        mode=mode,
    )
    return project


def _record_recent(project: Project) -> None:
    store = RecentsStore.load()
    store.upsert(
        RecentProject(
            project_dir=str(project.project_dir),
            name=project.name,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
        )
    )
    store.save()


def test_collect_entries_returns_attached_projects(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    lore = _make_lore_book(tmp_path)
    try:
        project = _make_project_attached(
            tiny_epub_factory,
            tmp_path,
            lore,
            project_name="proj-attached",
            mode=AttachedLoreMode.WRITABLE,
        )
        try:
            _record_recent(project)
            project_dir = project.project_dir
        finally:
            project.close()

        screen = SeriesScreen(lore)
        entries = list(screen._collect_entries())
        assert len(entries) == 1
        entry = entries[0]
        assert entry.recent.project_dir == str(project_dir)
        assert entry.mode == AttachedLoreMode.WRITABLE
        assert entry.stats.segment_count >= 1
    finally:
        lore.close()


def test_collect_entries_skips_unrelated_projects(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    lore_a = _make_lore_book(tmp_path, name="Series A")
    lore_b = LoreBook.create(
        out_dir=tmp_path / "lore-b",
        name="Series B",
        source_lang="en",
        target_lang="pt",
    )
    try:
        project = _make_project_attached(
            tiny_epub_factory,
            tmp_path,
            lore_b,
            project_name="proj-b-only",
        )
        try:
            _record_recent(project)
        finally:
            project.close()

        screen_a = SeriesScreen(lore_a)
        assert list(screen_a._collect_entries()) == []
        screen_b = SeriesScreen(lore_b)
        assert len(list(screen_b._collect_entries())) == 1
    finally:
        lore_a.close()
        lore_b.close()


def test_collect_entries_handles_empty_recents(tmp_path: Path) -> None:
    lore = _make_lore_book(tmp_path)
    try:
        screen = SeriesScreen(lore)
        assert list(screen._collect_entries()) == []
    finally:
        lore.close()


def test_collect_entries_skips_missing_project_dirs(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """If a recent points at a deleted folder, it must not crash the rollup."""
    lore = _make_lore_book(tmp_path)
    try:
        project = _make_project_attached(
            tiny_epub_factory,
            tmp_path,
            lore,
            project_name="proj-doomed",
        )
        try:
            _record_recent(project)
            doomed_dir = project.project_dir
        finally:
            project.close()

        # Nuke the project directory after recording it as a recent.
        import shutil

        shutil.rmtree(doomed_dir)

        screen = SeriesScreen(lore)
        entries = list(screen._collect_entries())
        assert entries == []
    finally:
        lore.close()


@pytest.mark.asyncio
async def test_series_screen_mounts_and_renders_summary(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    from epublate.app.main import EpublateApp

    lore = _make_lore_book(tmp_path)
    try:
        project = _make_project_attached(
            tiny_epub_factory,
            tmp_path,
            lore,
            project_name="proj-mount",
        )
        try:
            _record_recent(project)
        finally:
            project.close()

        screen = SeriesScreen(lore)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Static

            body = pilot.app.screen.query_one("#series-summary-body", Static)
            rendered = str(body.render())
            assert "1" in rendered  # at least one project counted
            assert "project(s)" in rendered
    finally:
        lore.close()
