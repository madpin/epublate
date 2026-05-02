"""Pipeline integration with attached Lore Books (PRD §4.3 / F-LB-10 phase 3)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from epublate.core.pipeline import (
    TranslateOptions,
    _load_glossary_view,
    translate_segment,
)
from epublate.core.project import Project
from epublate.db import repo
from epublate.db.schema import AttachedLoreMode, GlossaryStatus
from epublate.llm.mock import MockLLMProvider
from epublate.lore import LoreBook


def _basic_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1>"
                "<p>Geralt rode through Kaer Morhen at dusk.</p>"
                "<p>Yennefer waited on the parapet.</p>",
            )
        ]
    )
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def _seg_with(project: Project, needle: str) -> repo.SegmentRow:
    for chap in repo.list_chapters(project.engine, project.project_id):
        for seg in repo.list_segments(project.engine, chap.id):
            if needle in seg.source_text:
                return seg
    raise AssertionError(f"no segment contains {needle!r}")


def test_load_glossary_view_merges_attached_entries(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _basic_project(tiny_epub_factory, tmp_path)
    try:
        # Project glossary: only Yennefer
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Yennefer",
            target_term="Yennefer",
            type="character",
            status=GlossaryStatus.LOCKED,
        )
        # Lore Book glossary: Geralt + a duplicate Yennefer (should be deduped)
        lore_dir = tmp_path / "lore.epublate-lore"
        book = LoreBook.create(
            out_dir=lore_dir,
            name="Series Lore",
            source_lang="en",
            target_lang="pt",
        )
        try:
            repo.create_glossary_entry(
                book.engine,
                project_id=book.project_id,
                source_term="Geralt",
                target_term="Geralt",
                type="character",
                status=GlossaryStatus.LOCKED,
            )
            repo.create_glossary_entry(
                book.engine,
                project_id=book.project_id,
                source_term="Yennefer",
                target_term="Yennefer",
                type="character",
                status=GlossaryStatus.LOCKED,
            )
        finally:
            book.close()

        repo.attach_lore_book(
            project.engine,
            project_id=project.project_id,
            lore_path=str(lore_dir),
        )

        view = _load_glossary_view(project.engine, project_id=project.project_id)
        try:
            terms = sorted({(e.source_term or "", e.entry.type) for e in view.entries})
            assert ("Geralt", "character") in terms
            assert ("Yennefer", "character") in terms
            assert len(view.entries) == 2  # dedupe of Yennefer worked
            # The own_entry_ids set should contain only the project's Yennefer.
            assert len(view.own_entry_ids) == 1
        finally:
            view.close()
    finally:
        project.close()


def test_writable_attached_lore_receives_auto_proposals(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _basic_project(tiny_epub_factory, tmp_path)
    lore_dir = tmp_path / "lore.epublate-lore"
    book = LoreBook.create(
        out_dir=lore_dir,
        name="Series Lore",
        source_lang="en",
        target_lang="pt",
    )
    book.close()

    try:
        repo.attach_lore_book(
            project.engine,
            project_id=project.project_id,
            lore_path=str(lore_dir),
            mode=AttachedLoreMode.WRITABLE,
        )

        provider = MockLLMProvider()
        provider.set_response(
            json.dumps(
                {
                    "target": "Geralt cavalgou por Kaer Morhen ao crepúsculo.",
                    "new_entities": [
                        {"type": "character", "source": "Geralt"},
                        {"type": "place", "source": "Kaer Morhen"},
                    ],
                }
            )
        )
        seg = _seg_with(project, "Geralt")
        outcome = translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=seg,
            provider=provider,
            options=TranslateOptions(model="gpt-mock", auto_propose=True),
        )

        # The proposed entries land in the *lore book* DB, not the project.
        assert len(outcome.proposed_entry_ids) == 2

        project_entries = repo.list_glossary_entries(project.engine, project.project_id)
        assert project_entries == []

        reopened = LoreBook.open(lore_dir)
        try:
            lore_entries = repo.list_glossary_entries(
                reopened.engine, reopened.project_id
            )
            sources = sorted(e.source_term for e in lore_entries if e.source_term)
            assert sources == ["Geralt", "Kaer Morhen"]
        finally:
            reopened.close()
    finally:
        project.close()


def test_read_only_attached_lore_does_not_receive_proposals(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _basic_project(tiny_epub_factory, tmp_path)
    lore_dir = tmp_path / "lore.epublate-lore"
    book = LoreBook.create(
        out_dir=lore_dir,
        name="Series Lore",
        source_lang="en",
        target_lang="pt",
    )
    book.close()

    try:
        repo.attach_lore_book(
            project.engine,
            project_id=project.project_id,
            lore_path=str(lore_dir),
            mode=AttachedLoreMode.READ_ONLY,
        )

        provider = MockLLMProvider()
        provider.set_response(
            json.dumps(
                {
                    "target": "Geralt cavalgou por Kaer Morhen ao crepúsculo.",
                    "new_entities": [{"type": "character", "source": "Geralt"}],
                }
            )
        )
        seg = _seg_with(project, "Geralt")
        translate_segment(
            engine=project.engine,
            project_id=project.project_id,
            source_lang="en",
            target_lang="pt",
            style_guide=None,
            segment=seg,
            provider=provider,
            options=TranslateOptions(model="gpt-mock", auto_propose=True),
        )

        # Project should have the new entry...
        project_entries = repo.list_glossary_entries(project.engine, project.project_id)
        assert any(e.source_term == "Geralt" for e in project_entries)

        # ...and the read-only attached book should be empty.
        reopened = LoreBook.open(lore_dir)
        try:
            assert (
                repo.list_glossary_entries(reopened.engine, reopened.project_id) == []
            )
        finally:
            reopened.close()
    finally:
        project.close()


def test_load_glossary_view_skips_missing_lore_path(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _basic_project(tiny_epub_factory, tmp_path)
    try:
        repo.attach_lore_book(
            project.engine,
            project_id=project.project_id,
            lore_path=str(tmp_path / "does-not-exist.epublate-lore"),
        )
        view = _load_glossary_view(project.engine, project_id=project.project_id)
        try:
            assert view.entries == []
            assert view.writable_lore_engine is None
        finally:
            view.close()
    finally:
        project.close()
