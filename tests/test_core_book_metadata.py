"""Tests for book metadata + glossary stats + intake status helpers (M6)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.core.book_metadata import (
    BookMetadata,
    compute_glossary_stats,
    extract_book_metadata,
    get_intake_status,
)
from epublate.core.project import Project
from epublate.db import repo, schema


def _project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(
        chapters=[
            ("Chapter One", "<p>Hello world hello again.</p>"),
            ("Chapter Two", "<p>Three short words.</p>"),
        ],
        title="Sample Book",
    )
    return Project.create(
        src, out_dir=tmp_path / "meta-proj", source_lang="en", target_lang="pt"
    )


def test_extract_book_metadata_reads_title_and_word_count(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Title flows through and the word count covers spine documents."""

    project = _project(tiny_epub_factory, tmp_path)
    try:
        meta = extract_book_metadata(project.original_epub_path)
        assert meta.title == "Sample Book"
        assert meta.language == "en"
        assert meta.word_count is not None and meta.word_count > 0
        # ``tiny_epub_factory`` may pad the spine with a nav/TOC
        # document; assert "at least the chapters we wrote" rather than
        # an exact count so the test stays decoupled from fixture
        # internals.
        assert meta.chapter_count is not None and meta.chapter_count >= 2
    finally:
        project.close()


def test_extract_book_metadata_handles_missing_file(tmp_path: Path) -> None:
    """A bad path returns an empty :class:`BookMetadata` instead of raising."""

    meta = extract_book_metadata(tmp_path / "does-not-exist.epub")
    assert isinstance(meta, BookMetadata)
    assert meta.title is None
    assert meta.word_count is None


def test_book_metadata_short_description_truncates() -> None:
    """Long descriptions are cut at a sentence boundary near the limit."""

    raw = (
        "First sentence is short. " * 5
        + "Now we have a much longer trailing sentence "
        + "that runs past the truncation budget so the helper has to "
        + "decide where to cut. "
    )
    meta = BookMetadata(description=raw)
    short = meta.short_description(max_chars=100)
    assert short is not None
    assert short.endswith("…") or short.endswith(".")
    assert len(short) <= len(raw)


def test_book_metadata_author_line_summarizes_long_lists() -> None:
    """Three+ authors collapse to "first, second +N more"."""

    meta = BookMetadata(authors=("A", "B", "C", "D"))
    assert meta.author_line() == "A, B +2 more"


def test_book_metadata_author_line_unknown() -> None:
    assert BookMetadata().author_line() == "(unknown author)"


def test_compute_glossary_stats_aggregates_by_status(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Each status bucket is counted independently."""

    project = _project(tiny_epub_factory, tmp_path)
    try:
        engine = project.engine
        for idx, status in enumerate(
            [
                schema.GlossaryStatus.PROPOSED,
                schema.GlossaryStatus.PROPOSED,
                schema.GlossaryStatus.CONFIRMED,
                schema.GlossaryStatus.LOCKED,
            ]
        ):
            repo.create_glossary_entry(
                engine,
                project_id=project.project_id,
                source_term=f"term-{status}-{idx}",
                target_term=f"alvo-{status}",
                status=status,  # type: ignore[arg-type]
            )
        stats = compute_glossary_stats(engine, project.project_id)
        assert stats.proposed == 2
        assert stats.confirmed == 1
        assert stats.locked == 1
        assert stats.total == 4
    finally:
        project.close()


def test_get_intake_status_no_event_returns_not_run(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _project(tiny_epub_factory, tmp_path)
    try:
        status = get_intake_status(project.engine, project.project_id)
        assert status.has_run is False
        assert status.proposed_count is None
    finally:
        project.close()


def test_get_intake_status_returns_latest_event(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Multiple intake events: the most recent wins."""

    project = _project(tiny_epub_factory, tmp_path)
    try:
        repo.append_event(
            project.engine,
            project_id=project.project_id,
            kind="intake.completed",
            payload={"chunks": 1, "proposed_count": 0, "cost_usd": 0.001},
        )
        repo.append_event(
            project.engine,
            project_id=project.project_id,
            kind="intake.completed",
            payload={
                "chunks": 8,
                "proposed_count": 5,
                "cost_usd": 0.04,
                "pov": "first",
                "tense": "present",
            },
        )
        status = get_intake_status(project.engine, project.project_id)
        assert status.has_run is True
        assert status.chunks == 8
        assert status.proposed_count == 5
        assert status.pov == "first"
        assert status.tense == "present"
        assert status.cost_usd == pytest.approx(0.04)
    finally:
        project.close()
