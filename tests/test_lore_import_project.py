"""Tests for ``import_project_glossary`` (PRD F-LB-10)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.core.project import Project
from epublate.db import repo
from epublate.lore import (
    LoreBook,
    ProjectImportConflict,
    ProjectImportSummary,
    apply_conflict_resolution,
    import_project_glossary,
)


def _make_lore_book(
    tmp_path: Path,
    *,
    name: str = "Lore Bootstrap",
    sub: str = "lore",
) -> LoreBook:
    return LoreBook.create(
        out_dir=tmp_path / f"{sub}.epublate-lore",
        name=name,
        source_lang="en",
        target_lang="pt",
    )


def _make_project(
    tiny_factory: Callable[..., Path],
    tmp_path: Path,
    *,
    sub: str = "proj",
) -> Project:
    src = tiny_factory(
        chapters=[
            (
                "Chap",
                "<h1>Chap</h1><p>Hello.</p>",
            )
        ]
    )
    return Project.create(
        src,
        out_dir=tmp_path / sub,
        source_lang="en",
        target_lang="pt",
    )


def _seed_project_glossary(
    project: Project,
    rows: list[dict[str, object]],
) -> None:
    """Convenience: insert the given dicts as glossary entries."""

    for row in rows:
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term=row.get("source_term"),
            target_term=row["target_term"],
            type=row.get("type", "character"),
            status=row.get("status", "confirmed"),
            notes=row.get("notes"),
            source_aliases=list(row.get("source_aliases") or []),
            target_aliases=list(row.get("target_aliases") or []),
            source_known=row.get("source_known", row.get("source_term") is not None),
        )


def _entry_by_source(
    book: LoreBook, source_term: str, type_: str = "character"
) -> repo.GlossaryEntryWithAliases | None:
    found = repo.find_glossary_entry_by_source_term(
        book.engine,
        project_id=book.project_id,
        source_term=source_term,
        type=type_,
    )
    if found is None:
        return None
    return repo.get_glossary_entry(book.engine, found.id)


def test_import_into_empty_lore_book_creates_all_entries(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_project_glossary(
            project,
            [
                {"source_term": "Geralt", "target_term": "Geralt", "type": "character"},
                {
                    "source_term": "Kaer Morhen",
                    "target_term": "Kaer Morhen",
                    "type": "place",
                    "status": "locked",
                },
            ],
        )
        project_dir = project.project_dir
    finally:
        project.close()

    lore = _make_lore_book(tmp_path)
    try:
        summary = import_project_glossary(
            lore, src_project_dir=project_dir, policy="collect"
        )
        assert isinstance(summary, ProjectImportSummary)
        assert summary.created == 2
        assert summary.updated == 0
        assert summary.skipped == 0
        assert summary.conflicts == []
        assert _entry_by_source(lore, "Geralt") is not None
        assert _entry_by_source(lore, "Kaer Morhen", "place") is not None
    finally:
        lore.close()


def test_import_target_only_entries_always_insert(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_project_glossary(
            project,
            [
                {
                    "source_term": None,
                    "target_term": "Geralt de Rívia",
                    "type": "character",
                    "source_known": False,
                    "status": "locked",
                },
            ],
        )
        project_dir = project.project_dir
    finally:
        project.close()

    lore = _make_lore_book(tmp_path)
    try:
        summary = import_project_glossary(
            lore, src_project_dir=project_dir, policy="collect"
        )
        assert summary.created == 1
        assert summary.target_only_inserts == 1
        # A second import of the same target-only row inserts again — different
        # lore books may pin the same proper noun for unrelated entities, so
        # we don't try to dedupe by target_term.
        again = import_project_glossary(
            lore, src_project_dir=project_dir, policy="collect"
        )
        assert again.created == 1
        assert again.target_only_inserts == 1
        entries = repo.list_glossary_entries(lore.engine, lore.project_id)
        assert sum(1 for e in entries if e.target_term == "Geralt de Rívia") == 2
    finally:
        lore.close()


def test_import_skip_policy_keeps_destination(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_project_glossary(
            project,
            [
                {
                    "source_term": "Geralt",
                    "target_term": "Geralt the Witcher",
                    "type": "character",
                },
            ],
        )
        project_dir = project.project_dir
    finally:
        project.close()

    lore = _make_lore_book(tmp_path)
    try:
        repo.create_glossary_entry(
            lore.engine,
            project_id=lore.project_id,
            source_term="Geralt",
            target_term="Geralt de Rívia",
            type="character",
            status="locked",
        )
        summary = import_project_glossary(
            lore, src_project_dir=project_dir, policy="skip"
        )
        assert summary.skipped == 1
        assert summary.updated == 0
        assert summary.conflicts == []
        existing = _entry_by_source(lore, "Geralt")
        assert existing is not None
        assert existing.target_term == "Geralt de Rívia"
        assert existing.status == "locked"
    finally:
        lore.close()


def test_import_overwrite_policy_replaces_destination(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_project_glossary(
            project,
            [
                {
                    "source_term": "Geralt",
                    "target_term": "Geralt the Witcher",
                    "type": "character",
                    "status": "confirmed",
                    "target_aliases": ["Lobo Branco"],
                },
            ],
        )
        project_dir = project.project_dir
    finally:
        project.close()

    lore = _make_lore_book(tmp_path)
    try:
        repo.create_glossary_entry(
            lore.engine,
            project_id=lore.project_id,
            source_term="Geralt",
            target_term="Geralt de Rívia",
            type="character",
            status="locked",
        )
        summary = import_project_glossary(
            lore, src_project_dir=project_dir, policy="overwrite"
        )
        assert summary.updated == 1
        assert summary.skipped == 0
        existing = _entry_by_source(lore, "Geralt")
        assert existing is not None
        assert existing.target_term == "Geralt the Witcher"
        assert existing.status == "confirmed"
        assert "Lobo Branco" in existing.target_aliases
    finally:
        lore.close()


def test_import_collect_policy_returns_conflicts_without_writing(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_project_glossary(
            project,
            [
                {
                    "source_term": "Geralt",
                    "target_term": "Geralt the Witcher",
                    "type": "character",
                },
                {
                    "source_term": "Yennefer",
                    "target_term": "Yennefer",
                    "type": "character",
                },
            ],
        )
        project_dir = project.project_dir
    finally:
        project.close()

    lore = _make_lore_book(tmp_path)
    try:
        repo.create_glossary_entry(
            lore.engine,
            project_id=lore.project_id,
            source_term="Geralt",
            target_term="Geralt de Rívia",
            type="character",
            status="locked",
        )
        summary = import_project_glossary(
            lore, src_project_dir=project_dir, policy="collect"
        )
        assert summary.created == 1  # Yennefer
        assert summary.updated == 0
        assert summary.skipped == 0
        assert len(summary.conflicts) == 1
        c = summary.conflicts[0]
        assert isinstance(c, ProjectImportConflict)
        assert c.source_term == "Geralt"
        assert c.existing_target == "Geralt de Rívia"
        assert c.incoming_target == "Geralt the Witcher"

        # Conflict not yet applied: destination still has the original.
        existing = _entry_by_source(lore, "Geralt")
        assert existing is not None
        assert existing.target_term == "Geralt de Rívia"
    finally:
        lore.close()


def test_apply_conflict_resolution_use_incoming(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_project_glossary(
            project,
            [
                {
                    "source_term": "Geralt",
                    "target_term": "Geralt the Witcher",
                    "type": "character",
                    "status": "confirmed",
                },
            ],
        )
        project_dir = project.project_dir
    finally:
        project.close()

    lore = _make_lore_book(tmp_path)
    try:
        repo.create_glossary_entry(
            lore.engine,
            project_id=lore.project_id,
            source_term="Geralt",
            target_term="Geralt de Rívia",
            type="character",
            status="locked",
        )
        summary = import_project_glossary(
            lore, src_project_dir=project_dir, policy="collect"
        )
        assert len(summary.conflicts) == 1
        apply_conflict_resolution(
            lore, conflict=summary.conflicts[0], action="use_incoming"
        )
        existing = _entry_by_source(lore, "Geralt")
        assert existing is not None
        assert existing.target_term == "Geralt the Witcher"
        assert existing.status == "confirmed"
    finally:
        lore.close()


def test_apply_conflict_resolution_keep_existing_is_noop(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_project_glossary(
            project,
            [
                {
                    "source_term": "Geralt",
                    "target_term": "Geralt the Witcher",
                    "type": "character",
                },
            ],
        )
        project_dir = project.project_dir
    finally:
        project.close()

    lore = _make_lore_book(tmp_path)
    try:
        repo.create_glossary_entry(
            lore.engine,
            project_id=lore.project_id,
            source_term="Geralt",
            target_term="Geralt de Rívia",
            type="character",
            status="locked",
        )
        summary = import_project_glossary(
            lore, src_project_dir=project_dir, policy="collect"
        )
        apply_conflict_resolution(
            lore, conflict=summary.conflicts[0], action="keep_existing"
        )
        existing = _entry_by_source(lore, "Geralt")
        assert existing is not None
        assert existing.target_term == "Geralt de Rívia"
    finally:
        lore.close()


def test_apply_conflict_resolution_rejects_unknown_action(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_project_glossary(
            project,
            [
                {
                    "source_term": "Geralt",
                    "target_term": "Geralt the Witcher",
                    "type": "character",
                },
            ],
        )
        project_dir = project.project_dir
    finally:
        project.close()

    lore = _make_lore_book(tmp_path)
    try:
        repo.create_glossary_entry(
            lore.engine,
            project_id=lore.project_id,
            source_term="Geralt",
            target_term="Geralt de Rívia",
            type="character",
            status="locked",
        )
        summary = import_project_glossary(
            lore, src_project_dir=project_dir, policy="collect"
        )
        with pytest.raises(ValueError):
            apply_conflict_resolution(
                lore,
                conflict=summary.conflicts[0],
                action="bogus",  # type: ignore[arg-type]
            )
    finally:
        lore.close()


def test_re_import_identical_payload_is_silent_skip(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Re-importing the same project twice must not raise spurious conflicts."""
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_project_glossary(
            project,
            [
                {
                    "source_term": "Geralt",
                    "target_term": "Geralt de Rívia",
                    "type": "character",
                    "status": "confirmed",
                },
            ],
        )
        project_dir = project.project_dir
    finally:
        project.close()

    lore = _make_lore_book(tmp_path)
    try:
        first = import_project_glossary(
            lore, src_project_dir=project_dir, policy="collect"
        )
        assert first.created == 1
        second = import_project_glossary(
            lore, src_project_dir=project_dir, policy="collect"
        )
        assert second.created == 0
        assert second.updated == 0
        assert second.skipped == 1
        assert second.conflicts == []
    finally:
        lore.close()
