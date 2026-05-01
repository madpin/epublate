"""Tests for the Project lifecycle service (PRD §4.7, §7.1, §7.6 / M1)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.core.project import (
    ORIGINAL_EPUB_NAME,
    PROJECT_DB_SUFFIX,
    Project,
    open_project,
)
from epublate.db import repo
from epublate.errors import ConfigurationError


def test_create_populates_chapters_and_segments(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory(
        chapters=[
            ("One", "<h1>One</h1><p>Hello, <em>brave</em> world.</p>"),
            ("Two", "<h1>Two</h1><p>Two-paragraph chapter.</p><p>End.</p>"),
        ],
    )
    out_dir = tmp_path / "proj"
    project = Project.create(src, out_dir=out_dir, source_lang="en", target_lang="pt")
    try:
        assert (out_dir / ORIGINAL_EPUB_NAME).is_file()
        assert any(out_dir.glob(f"*{PROJECT_DB_SUFFIX}"))

        chapters = repo.list_chapters(project.engine, project.project_id)
        # nav + 2 content chapters from the spine
        assert len(chapters) == 3

        # Content chapters carry their h1 + paragraphs as segments.
        content = [c for c in chapters if c.href != "nav.xhtml"]
        all_segs = [
            seg for c in content for seg in repo.list_segments(project.engine, c.id)
        ]
        sources = [s.source_text for s in all_segs]
        assert any("Hello" in s for s in sources)
        assert any("Two-paragraph" in s for s in sources)
        assert any("End." in s for s in sources)

        events = repo.list_events(project.engine, project.project_id)
        kinds = [e.kind for e in events]
        assert "project.created" in kinds
        assert "chapters.imported" in kinds
    finally:
        project.close()


def test_create_refuses_non_empty_directory(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    out_dir = tmp_path / "proj"
    out_dir.mkdir()
    (out_dir / "preexisting.txt").write_text("hi")
    with pytest.raises(ConfigurationError):
        Project.create(src, out_dir=out_dir, source_lang="en", target_lang="pt")


def test_open_project_round_trip(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    out_dir = tmp_path / "proj"
    project = Project.create(src, out_dir=out_dir, source_lang="en", target_lang="pt")
    project.close()

    with open_project(out_dir) as reopened:
        assert reopened.target_lang == "pt"
        chapters = repo.list_chapters(reopened.engine, reopened.project_id)
        assert chapters


def test_create_backfills_chapter_titles_from_calibre_headings(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Chapters whose only heading is a Calibre-style ``<div><span class='bold'>``
    block must still land in the DB with a non-NULL ``title`` so the
    Reader sidebar shows the curator a real chapter name."""

    src = tiny_epub_factory(
        chapters=[
            (
                "",
                '<div class="calibre14">'
                '<span class="calibre6"><span class="bold">Introduction</span></span>'
                "</div>"
                '<div class="calibre3">'
                '<span class="calibre6">Real prose body text here.</span>'
                "</div>",
            )
        ],
    )
    out_dir = tmp_path / "proj"
    project = Project.create(src, out_dir=out_dir, source_lang="en", target_lang="pt")
    try:
        chapters = repo.list_chapters(project.engine, project.project_id)
        body_chapters = [c for c in chapters if c.href != "nav.xhtml"]
        assert any(c.title == "Introduction" for c in body_chapters), [
            c.title for c in body_chapters
        ]
    finally:
        project.close()


def test_open_backfills_missing_titles_from_calibre_headings(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Older project DBs with ``title=NULL`` for chapters that DO have
    a Calibre-style heading should be patched up on the next
    :meth:`Project.open` so the user doesn't have to re-create the
    project just to get readable sidebar labels."""

    src = tiny_epub_factory(
        chapters=[
            (
                "",
                '<div class="calibre14">'
                '<span class="calibre6"><span class="bold">Chapter Zero</span></span>'
                "</div>"
                '<div class="calibre3">'
                '<span class="calibre6">Body text.</span>'
                "</div>",
            )
        ],
    )
    out_dir = tmp_path / "proj"
    project = Project.create(src, out_dir=out_dir, source_lang="en", target_lang="pt")
    try:
        # Simulate a pre-upgrade project where the title extractor
        # never noticed the heading (NULL title persisted on disk).
        from sqlalchemy import update

        from epublate.db import schema

        with project.engine.begin() as conn:
            conn.execute(update(schema.chapter).values(title=None))
        before = repo.list_chapters(project.engine, project.project_id)
        assert all(c.title is None for c in before)
    finally:
        project.close()

    with open_project(out_dir) as reopened:
        chapters = repo.list_chapters(reopened.engine, reopened.project_id)
        body_chapters = [c for c in chapters if c.href != "nav.xhtml"]
        assert any(c.title == "Chapter Zero" for c in body_chapters), [
            c.title for c in body_chapters
        ]


def test_export_round_trips_segments(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1>"
                "<p>One <em>emphasized</em> paragraph.</p>"
                '<p>Another with a <a href="#x">link</a>.</p>',
            )
        ]
    )
    out_dir = tmp_path / "proj"
    out_path = tmp_path / "translated.epub"
    project = Project.create(src, out_dir=out_dir, source_lang="en", target_lang="pt")
    try:
        project.export(out_path)
    finally:
        project.close()
    assert out_path.is_file()

    # Reopen the exported ePub and confirm segments survived. The
    # exported chapters now carry ``<html lang="pt">`` (we genuinely
    # advertise the file as such), so an adapter with
    # ``target_lang="pt"`` would correctly skip every block as
    # already-translated and the test would only ever pass by
    # accident. Drop the filter so we're checking structural survival
    # of the source content, not language-aware filtering.
    from epublate.formats.epub import EpubAdapter

    adapter = EpubAdapter(target_lang=None)
    book = adapter.load(out_path)
    sources: list[str] = []
    for doc in adapter.iter_chapters(book):
        if doc.tree is None:
            continue
        segs = adapter.segment(doc, chapter_id=f"ch-{doc.spine_idx}")
        sources.extend(s.source_text for s in segs)
    assert any("emphasized" in s for s in sources)
    assert any("[[T0]]" in s for s in sources)


def test_open_missing_db_raises(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ConfigurationError):
        Project.open(empty)
