"""Project lifecycle service (PRD §4.7, §7.1, §7.6).

A *project* is a self-contained folder that owns:

* the original ePub (copied verbatim, never mutated),
* a SQLite database (``<book-stem>.epublate``) holding chapters, segments,
  glossary, llm_call cache, and the append-only event log.

This module is the single entry point for creating, opening, and exporting
projects. The TUI and CLI go through here; the format adapter and DB
repository stay free of filesystem layout concerns.
"""

from __future__ import annotations

import logging
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import update
from sqlalchemy.engine import Engine

from epublate.core.extractor import IntakeOptions, IntakeSummary, run_book_intake
from epublate.core.style import DEFAULT_STYLE_PROFILE, resolve_style_guide
from epublate.db import connect, repo, schema
from epublate.errors import ConfigurationError
from epublate.formats.base import ChapterDoc, Segment
from epublate.formats.epub import EpubAdapter, toc_title_map
from epublate.formats.epubcheck import EpubCheckReport, run_epubcheck
from epublate.glossary import io as glossary_io
from epublate.llm.base import LLMProvider

ORIGINAL_EPUB_NAME = "original.epub"
PROJECT_DB_SUFFIX = ".epublate"

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Project:
    """In-memory handle to an opened project on disk.

    Use :meth:`Project.create` to bootstrap a new project from an ePub source
    and :meth:`Project.open` to reopen an existing one. Both return a fully
    initialized handle; the caller is responsible for closing the underlying
    engine via :meth:`close` (or using :func:`open_project` as a context
    manager).

    ``last_intake_summary`` is populated by :meth:`Project.create` when the
    M5 book-intake pass ran (PRD §7.1 step 5); the CLI / Dashboard read it
    to surface a one-line summary of the helper LLM's proposals.
    """

    project_dir: Path
    project_id: str
    name: str
    source_lang: str
    target_lang: str
    db_path: Path
    engine: Engine
    last_intake_summary: IntakeSummary | None = None
    last_epubcheck_report: EpubCheckReport | None = None

    @classmethod
    def create(
        cls,
        source_epub: Path,
        *,
        out_dir: Path,
        source_lang: str,
        target_lang: str,
        name: str | None = None,
        starter_glossary: Path | None = None,
        intake: IntakeOptions | None = None,
        intake_provider: LLMProvider | None = None,
        style_profile: str | None = DEFAULT_STYLE_PROFILE,
        style_guide: str | None = None,
    ) -> Project:
        """Initialize a new project folder from ``source_epub``.

        Layout:

            <out_dir>/
              original.epub          # immutable copy of the source
              <stem>.epublate        # SQLite DB (WAL)

        ``starter_glossary``, if provided, is a path to a JSON glossary
        file in the format produced by :func:`epublate.glossary.io.export_json`;
        entries are imported as ``proposed`` (or whatever their file
        status says) and recorded under a ``glossary.starter_imported``
        event so the curator can audit what landed (PRD §7.1).

        ``intake`` + ``intake_provider`` opt into the helper-LLM book
        intake pass (PRD §7.1 step 5 / M5). When both are provided, the
        helper LLM is asked to surface candidate proper-noun entities
        from the start of the book; results land as ``proposed`` entries
        the curator can promote/reject from the Glossary screen. The
        intake summary is attached to the returned project as
        :attr:`Project.last_intake_summary`. Failures within the intake
        pass are *not* fatal — the project is created either way.

        ``style_profile`` selects one of the shipped tone presets
        (PRD F-STYLE-1, see :mod:`epublate.core.style`) and pre-fills
        ``project.style_guide`` with the preset's prompt block so every
        translator call gets a consistent voice from the first segment.
        Pass ``None`` to opt out entirely (status quo, unspecified
        tone). ``style_guide`` overrides the preset's prompt block with
        free-form text — useful for the New Project modal's "edit the
        prefilled paragraph" flow. When the helper LLM intake runs and
        the curator left the default preset in place, the suggested
        profile (derived from the model's register / audience guess) is
        attached to :attr:`Project.last_intake_summary` so the
        dashboard can offer a one-click apply.

        Raises :class:`ConfigurationError` if ``out_dir`` already exists with
        files in it (we refuse to overwrite a populated folder).
        """

        source_epub = Path(source_epub).resolve()
        if not source_epub.is_file():
            raise ConfigurationError(f"source ePub not found: {source_epub}")

        out_dir = Path(out_dir).resolve()
        if out_dir.exists() and any(out_dir.iterdir()):
            raise ConfigurationError(
                f"refusing to create project in non-empty directory: {out_dir}"
            )
        out_dir.mkdir(parents=True, exist_ok=True)

        original_path = out_dir / ORIGINAL_EPUB_NAME
        shutil.copyfile(source_epub, original_path)

        stem = source_epub.stem or "project"
        db_path = out_dir / f"{stem}{PROJECT_DB_SUFFIX}"
        engine = connect(db_path)

        resolved_guide = resolve_style_guide(
            profile_id=style_profile, custom_text=style_guide
        )

        intake_summary: IntakeSummary | None = None
        try:
            project_name = name or stem
            project_row = repo.create_project(
                engine,
                name=project_name,
                source_lang=source_lang,
                target_lang=target_lang,
                source_path=str(original_path),
                style_profile=style_profile,
                style_guide=resolved_guide,
            )
            repo.append_event(
                engine,
                project_id=project_row.id,
                kind="project.created",
                payload={
                    "name": project_name,
                    "source_lang": source_lang,
                    "target_lang": target_lang,
                    "source_path": str(original_path),
                    "style_profile": style_profile,
                    "style_guide_set": resolved_guide is not None,
                },
            )
            _import_chapters(
                engine,
                project_id=project_row.id,
                source_epub=original_path,
                target_lang=target_lang,
            )
            if starter_glossary is not None:
                summary = glossary_io.import_starter(
                    engine,
                    project_id=project_row.id,
                    path=Path(starter_glossary),
                )
                repo.append_event(
                    engine,
                    project_id=project_row.id,
                    kind="glossary.starter_imported",
                    payload={
                        "path": str(starter_glossary),
                        "created": summary.created,
                        "skipped": summary.skipped,
                    },
                )
            if intake is not None and intake_provider is not None:
                intake_summary = run_book_intake(
                    engine=engine,
                    project_id=project_row.id,
                    source_lang=source_lang,
                    target_lang=target_lang,
                    provider=intake_provider,
                    options=intake,
                )
        except Exception:
            engine.dispose()
            raise

        project = cls(
            project_dir=out_dir,
            project_id=project_row.id,
            name=project_name,
            source_lang=source_lang,
            target_lang=target_lang,
            db_path=db_path,
            engine=engine,
        )
        project.last_intake_summary = intake_summary
        return project

    @property
    def style_guide(self) -> str | None:
        """Resolved prompt block currently fed to the translator.

        Reads from the project row each call so a Settings edit lands
        immediately in the next translate call. Returns ``None`` when the
        project has no tone configured (legacy projects predating M3+).
        """

        row = repo.get_project(self.engine, self.project_id)
        return row.style_guide if row is not None else None

    @property
    def style_profile(self) -> str | None:
        """Slug of the active tone preset (or ``None`` for custom / unset)."""

        row = repo.get_project(self.engine, self.project_id)
        return row.style_profile if row is not None else None

    def update_style(
        self,
        *,
        style_profile: str | None,
        custom_text: str | None = None,
    ) -> repo.ProjectRow:
        """Change the project's tone preset and resolved prompt block.

        ``custom_text`` overrides the preset's prompt block when truthy
        (the New Project / Settings modals pre-fill the TextArea with
        the preset and let the curator edit it). Pass
        ``style_profile=None, custom_text=None`` to clear the style
        entirely. Returns the refreshed project row so the caller can
        re-render UI without an extra round trip.
        """

        resolved = resolve_style_guide(
            profile_id=style_profile, custom_text=custom_text
        )
        return repo.update_project_style(
            self.engine,
            project_id=self.project_id,
            style_profile=style_profile,
            style_guide=resolved,
        )

    @classmethod
    def open(cls, project_dir: Path) -> Project:
        """Re-open an existing project folder."""

        project_dir = Path(project_dir).resolve()
        if not project_dir.is_dir():
            raise ConfigurationError(f"not a project directory: {project_dir}")
        db_path = _resolve_db_path(project_dir)
        engine = connect(db_path)
        try:
            projects = repo.list_projects(engine)
        except Exception:
            engine.dispose()
            raise
        if not projects:
            engine.dispose()
            raise ConfigurationError(f"no project row found in {db_path}")
        if len(projects) > 1:
            engine.dispose()
            raise ConfigurationError(
                f"multiple projects in {db_path}; expected exactly one"
            )
        row = projects[0]
        # Existing projects may have been imported before the title
        # extractor learned about Calibre-style headings or the EPUB
        # TOC fallback. Try once on every open to fill in the blanks
        # so the Reader sidebar reads naturally without requiring a
        # re-create. The pass is a no-op when nothing is missing.
        try:
            _backfill_chapter_titles(
                engine,
                project_id=row.id,
                source_epub=project_dir / ORIGINAL_EPUB_NAME,
                target_lang=row.target_lang,
            )
        except Exception as exc:
            _logger.warning(
                "chapter-title backfill skipped for %s: %s", project_dir, exc
            )
        return cls(
            project_dir=project_dir,
            project_id=row.id,
            name=row.name,
            source_lang=row.source_lang,
            target_lang=row.target_lang,
            db_path=db_path,
            engine=engine,
        )

    @property
    def original_epub_path(self) -> Path:
        return self.project_dir / ORIGINAL_EPUB_NAME

    def export(
        self,
        out_path: Path,
        *,
        epubcheck: bool = False,
    ) -> Path:
        """Write a (possibly partial) ePub to ``out_path``.

        Untranslated segments fall back to source text per F-IO-7. The export
        is atomic (write-temp-then-rename) and updates the OPF
        ``dc:language`` plus a provenance ``dc:contributor`` entry on the way
        out.

        When ``epubcheck=True`` the optional ``epubcheck`` extra is invoked
        post-write and its outcome is stored as ``last_epubcheck_report``
        and emitted as a ``project.epubcheck_completed`` event (PRD F-IO-6).
        Validation never blocks the write — the file is on disk by the
        time we shell out to the validator.
        """

        out_path = Path(out_path).resolve()
        adapter = EpubAdapter(target_lang=self.target_lang)
        book = adapter.load(self.original_epub_path)
        book.extras["target_lang"] = self.target_lang

        chapter_rows = repo.list_chapters(self.engine, self.project_id)
        chapter_by_spine: dict[int, repo.ChapterRow] = {
            r.spine_idx: r for r in chapter_rows
        }

        for doc in adapter.iter_chapters(book):
            chapter_row = chapter_by_spine.get(doc.spine_idx)
            if chapter_row is None or doc.tree is None:
                continue
            seg_rows = repo.list_segments(self.engine, chapter_row.id)
            segments: list[Segment] = [repo.segment_row_to(r) for r in seg_rows]
            if segments:
                adapter.reassemble(doc, segments)

        adapter.save(book, out_path)
        repo.append_event(
            self.engine,
            project_id=self.project_id,
            kind="project.exported",
            payload={"out_path": str(out_path)},
        )

        if epubcheck:
            report = run_epubcheck(out_path)
            self.last_epubcheck_report = report
            repo.append_event(
                self.engine,
                project_id=self.project_id,
                kind="project.epubcheck_completed",
                payload=report.event_payload(),
            )

        return out_path

    def close(self) -> None:
        self.engine.dispose()


@contextmanager
def open_project(project_dir: Path) -> Iterator[Project]:
    """Context manager around :meth:`Project.open`."""

    project = Project.open(project_dir)
    try:
        yield project
    finally:
        project.close()


def _resolve_db_path(project_dir: Path) -> Path:
    candidates = sorted(project_dir.glob(f"*{PROJECT_DB_SUFFIX}"))
    if not candidates:
        raise ConfigurationError(f"no *{PROJECT_DB_SUFFIX} file found in {project_dir}")
    if len(candidates) > 1:
        raise ConfigurationError(
            f"multiple *{PROJECT_DB_SUFFIX} files in {project_dir}; expected one"
        )
    return candidates[0]


def _import_chapters(
    engine: Engine,
    *,
    project_id: str,
    source_epub: Path,
    target_lang: str,
) -> None:
    """Segment every spine document and persist chapters + segments."""

    adapter = EpubAdapter(target_lang=target_lang)
    book = adapter.load(source_epub)
    toc_titles = toc_title_map(book)

    chapter_rows: list[repo.ChapterRow] = []
    segment_rows: list[repo.SegmentRow] = []
    docs_with_chapters: list[tuple[ChapterDoc, repo.ChapterRow]] = []

    for doc in adapter.iter_chapters(book):
        # Title resolution order: in-document heading (h1/.../Calibre)
        # first, then the EPUB's own TOC. The TOC is the most
        # authoritative source of "how the author named this file" but
        # in-document headings are usually what the curator sees while
        # reading, so we prefer them when both agree.
        title = doc.title or toc_titles.get(doc.href)
        chapter_rows.append(
            repo.ChapterRow(
                id=uuid.uuid4().hex,
                project_id=project_id,
                spine_idx=doc.spine_idx,
                href=doc.href,
                title=title,
                status=schema.ChapterStatus.PENDING,
            )
        )
        docs_with_chapters.append((doc, chapter_rows[-1]))

    for doc, chapter_row in docs_with_chapters:
        if doc.tree is None:
            continue
        adapter_segments = adapter.segment(doc, chapter_id=chapter_row.id)
        for seg in adapter_segments:
            segment_rows.append(repo.segment_row_from(seg))

    # Single transaction: chapters + segments + the audit event commit
    # together so a crash leaves a clean DB (db-and-persistence rule §2).
    with engine.begin() as conn:
        repo.bulk_insert_chapters(conn, chapter_rows)
        repo.bulk_insert_segments(conn, segment_rows)
        repo.append_event(
            conn,
            project_id=project_id,
            kind="chapters.imported",
            payload={
                "chapter_count": len(chapter_rows),
                "segment_count": len(segment_rows),
            },
        )
    _logger.info(
        "imported %d chapters / %d segments from %s",
        len(chapter_rows),
        len(segment_rows),
        source_epub,
    )


def _backfill_chapter_titles(
    engine: Engine,
    *,
    project_id: str,
    source_epub: Path,
    target_lang: str,
) -> None:
    """Fill in missing chapter titles for an already-imported project.

    Earlier imports (or imports against ePubs that hide their headings
    in Calibre-style ``<div><span class="bold">`` blocks) leave many
    chapter rows with ``title=None``, which makes the Reader sidebar
    read like a wall of "Chapter N" placeholders. Re-running the
    enriched extractor + TOC fallback against the source ePub fixes
    those rows without forcing the curator to re-create the project.

    Cheap by design: the function exits before touching the ePub when
    every chapter already has a title, and only updates rows whose
    title was previously ``NULL`` so the curator's manual edits (if we
    ever expose them) are preserved.
    """

    chapters = repo.list_chapters(engine, project_id)
    missing = [c for c in chapters if not c.title]
    if not missing:
        return
    if not source_epub.is_file():
        _logger.debug(
            "skipping title backfill for project %s: source ePub missing at %s",
            project_id,
            source_epub,
        )
        return

    adapter = EpubAdapter(target_lang=target_lang)
    book = adapter.load(source_epub)
    toc_titles = toc_title_map(book)
    by_href: dict[str, repo.ChapterRow] = {c.href: c for c in missing}
    fresh_titles: dict[str, str] = {}
    for doc in adapter.iter_chapters(book):
        chap = by_href.get(doc.href)
        if chap is None:
            continue
        title = doc.title or toc_titles.get(doc.href)
        if title:
            fresh_titles[chap.id] = title
    if not fresh_titles:
        return

    with engine.begin() as conn:
        for chapter_id, title in fresh_titles.items():
            conn.execute(
                update(schema.chapter)
                .where(schema.chapter.c.id == chapter_id)
                .values(title=title)
            )
    _logger.info(
        "backfilled %d chapter titles for project %s",
        len(fresh_titles),
        project_id,
    )


__all__ = [
    "ORIGINAL_EPUB_NAME",
    "PROJECT_DB_SUFFIX",
    "Project",
    "open_project",
]
