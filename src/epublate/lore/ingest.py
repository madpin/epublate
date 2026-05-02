"""Ingest a *source-language* ePub into a Lore Book (PRD §4.3 / F-LB-10).

This is the cheap path: we segment the ePub the same way we segment a
project's source, then run the existing helper-LLM extractor over a
chunked subset of the segments. The Lore Book's glossary tables fill
up with ``proposed`` source-keyed entries (just like a regular project's
intake pass), and a ``lore_source`` row records what was ingested.

We deliberately reuse :func:`epublate.core.extractor.run_book_intake`
under the hood: the only difference between a project intake and a
Lore Book source ingest is *what* DB the chapters/segments land in.
The Lore Book temporarily borrows the project DB's chapter/segment
schema so the extractor can read segments back the same way it does
for translation projects.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from epublate.core.extractor import (
    DEFAULT_INTAKE_MAX_SEGMENTS,
    IntakeOptions,
    IntakeSummary,
    run_book_intake,
)
from epublate.db import repo, schema
from epublate.errors import ConfigurationError
from epublate.formats.epub import EpubAdapter, toc_title_map
from epublate.llm.base import LLMProvider
from epublate.lore import repo as lore_repo
from epublate.lore.lore import LoreBook

_logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class LoreIngestSummary:
    """Outcome of one Lore Book ingest call.

    ``source_id`` is the row in ``lore_source`` we just inserted;
    ``intake_summary`` carries the extractor numbers (chunks, cost,
    proposed_count). ``proposed_count`` is mirrored at the top level
    so callers don't need to drill into the intake summary for the
    most common piece of information.
    """

    source_id: str
    proposed_count: int
    intake_summary: IntakeSummary | None


def ingest_source_epub(
    lore_book: LoreBook,
    *,
    epub_path: Path,
    provider: LLMProvider,
    helper_model: str,
    max_segments: int = DEFAULT_INTAKE_MAX_SEGMENTS,
    bypass_cache: bool = False,
    notes: str | None = None,
) -> LoreIngestSummary:
    """Run a source-language extractor pass against ``epub_path``.

    Steps:

    1. Stash a copy of the source ePub under the Lore Book's
       ``sources/`` directory (so re-runs are reproducible).
    2. Import the ePub's chapters and segments into the Lore Book DB
       under transient ``chapter`` / ``segment`` rows — the extractor
       wants to read from the existing schema, and a Lore Book DB
       carries the same tables.
    3. Run :func:`run_book_intake` against the Lore Book's project_id
       (where ``project_id`` is repurposed as ``lore_id``).
    4. Record the result in ``lore_source``.

    The transient chapter / segment rows stay in the Lore Book DB on
    purpose — they let a curator re-run the extractor with a different
    helper model later without re-parsing the ePub. The DB is
    Lore-Book-private so this doesn't pollute any translation project.
    """

    epub_path = Path(epub_path).resolve()
    if not epub_path.is_file():
        raise ConfigurationError(f"source ePub not found: {epub_path}")

    stash_path = lore_book.stash_source(epub_path)

    _import_ingest_chapters(
        lore_book=lore_book,
        source_epub=stash_path,
    )

    options = IntakeOptions(
        model=helper_model,
        max_segments=max_segments,
        bypass_cache=bypass_cache,
    )
    intake_summary: IntakeSummary | None = None
    try:
        intake_summary = run_book_intake(
            engine=lore_book.engine,
            project_id=lore_book.project_id,
            source_lang=lore_book.source_lang,
            target_lang=lore_book.target_lang,
            provider=provider,
            options=options,
        )
        proposed = intake_summary.proposed_count
        status = schema.LoreSourceStatus.INGESTED
    except Exception as exc:
        _logger.warning("lore source ingest failed for %s: %s", epub_path, exc)
        proposed = 0
        status = schema.LoreSourceStatus.FAILED

    source_row = lore_repo.insert_lore_source(
        lore_book.engine,
        project_id=lore_book.project_id,
        kind=schema.LoreSourceKind.SOURCE,
        epub_path=str(stash_path),
        status=status,
        entries_added=proposed,
        notes=notes,
    )
    repo.append_event(
        lore_book.engine,
        project_id=lore_book.project_id,
        kind="lore.source_ingested",
        payload={
            "source_id": source_row.id,
            "epub_path": str(stash_path),
            "proposed_count": proposed,
            "status": status,
        },
    )
    return LoreIngestSummary(
        source_id=source_row.id,
        proposed_count=proposed,
        intake_summary=intake_summary,
    )


def _import_ingest_chapters(
    *,
    lore_book: LoreBook,
    source_epub: Path,
) -> None:
    """Persist transient chapter / segment rows so the extractor can read them.

    Mirrors ``epublate.core.project._import_chapters`` but deliberately
    *doesn't* emit a ``chapters.imported`` event — Lore Book ingest
    has its own ``lore.source_ingested`` event that says everything
    the curator cares about. We also tag the chapter href so a re-run
    inserts a fresh batch instead of conflicting on the
    ``chapter_project_spine`` unique constraint.
    """

    adapter = EpubAdapter(target_lang=lore_book.target_lang)
    book = adapter.load(source_epub)
    toc_titles = toc_title_map(book)
    run_id = uuid.uuid4().hex[:8]

    chapter_rows: list[repo.ChapterRow] = []
    segment_rows: list[repo.SegmentRow] = []

    existing_offsets = repo.list_chapters(lore_book.engine, lore_book.project_id)
    base_idx = 1 + max((c.spine_idx for c in existing_offsets), default=-1)

    for offset, doc in enumerate(adapter.iter_chapters(book)):
        title = doc.title or toc_titles.get(doc.href)
        chapter_id = uuid.uuid4().hex
        chapter_rows.append(
            repo.ChapterRow(
                id=chapter_id,
                project_id=lore_book.project_id,
                spine_idx=base_idx + offset,
                href=f"{run_id}:{doc.href}",
                title=title,
                status=schema.ChapterStatus.PENDING,
            )
        )
        if doc.tree is None:
            continue
        for seg in adapter.segment(doc, chapter_id=chapter_id):
            segment_rows.append(repo.segment_row_from(seg))

    if not chapter_rows:
        return

    with lore_book.engine.begin() as conn:
        repo.bulk_insert_chapters(conn, chapter_rows)
        if segment_rows:
            repo.bulk_insert_segments(conn, segment_rows)


__all__ = [
    "LoreIngestSummary",
    "ingest_source_epub",
]
