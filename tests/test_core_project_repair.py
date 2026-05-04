"""Tests for :meth:`Project.repair_segmentation` (PRD §7.6 / orphan recovery).

The repair flow is the migration path for projects segmented before
the orphan-hoist fix: it walks every chapter, re-runs segmentation
against the original ePub, inserts segments that the older segmenter
missed, and rewrites ``host_path`` on existing rows whose XPath
shifted under the hoist. These tests exercise the three contracts
the repair makes:

* fresh projects are no-ops (idempotent),
* missing orphan rows get inserted, and
* translations on existing rows survive the re-host.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from sqlalchemy import delete, update

from epublate.core.project import Project
from epublate.db import repo, schema

_CALIBRE_BODY = (
    "<h1>Chapter</h1>"
    '<div class="calibre22">'
    '<span class="calibre10">Stranded paragraph that should be translated.</span>'
    '<div class="calibre26"><blockquote><p>Block-host quotation here.</p>'
    "</blockquote></div>"
    "</div>"
    "<p>Trailing standalone paragraph.</p>"
)


def test_repair_segmentation_is_noop_on_fresh_project(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory(
        chapters=[("One", "<h1>One</h1><p>Hello world.</p>")],
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        before = sum(
            len(repo.list_segments(project.engine, c.id))
            for c in repo.list_chapters(project.engine, project.project_id)
        )

        summary = project.repair_segmentation()

        assert summary.added == 0
        assert summary.rehosted == 0
        assert summary.chapters == []
        after = sum(
            len(repo.list_segments(project.engine, c.id))
            for c in repo.list_chapters(project.engine, project.project_id)
        )
        assert after == before
    finally:
        project.close()


def test_repair_inserts_segment_for_missing_orphan_row(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """If the older segmenter dropped a stranded ``<span>``, repair re-creates it.

    Simulated by deleting the orphan row from the segment table after
    creation; the repair pass should put it back with the same source
    text (and a fresh ``host_path`` pointing at the synthetic wrapper).
    """

    src = tiny_epub_factory(chapters=[("Calibre", _CALIBRE_BODY)])
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        chapters = [
            c
            for c in repo.list_chapters(project.engine, project.project_id)
            if c.href != "nav.xhtml"
        ]
        assert len(chapters) == 1
        chapter_id = chapters[0].id

        rows = repo.list_segments(project.engine, chapter_id)
        orphan_row = next(r for r in rows if "Stranded paragraph" in r.source_text)
        before_total = len(rows)

        with project.engine.begin() as conn:
            conn.execute(
                delete(schema.segment).where(schema.segment.c.id == orphan_row.id)
            )

        summary = project.repair_segmentation()

        assert summary.added == 1
        assert summary.rehosted == 0
        assert len(summary.chapters) == 1
        outcome = summary.chapters[0]
        assert outcome.chapter_id == chapter_id
        assert outcome.added == 1

        rows_after = repo.list_segments(project.engine, chapter_id)
        assert len(rows_after) == before_total
        assert any("Stranded paragraph" in r.source_text for r in rows_after)
    finally:
        project.close()


def test_repair_rehosts_existing_translation_without_losing_it(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Existing translations are preserved when ``host_path`` is refreshed.

    Simulated by mutating one row's ``inline_skeleton`` JSON envelope
    (the source of truth for ``host_path``) to point at a stale XPath,
    then verifying repair updates it back without touching
    ``target_text``.
    """

    src = tiny_epub_factory(chapters=[("Calibre", _CALIBRE_BODY)])
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        chapters = [
            c
            for c in repo.list_chapters(project.engine, project.project_id)
            if c.href != "nav.xhtml"
        ]
        chapter_id = chapters[0].id

        rows = repo.list_segments(project.engine, chapter_id)
        target_row = next(
            r for r in rows if "Trailing standalone paragraph" in r.source_text
        )

        repo.update_segment_translation(
            project.engine,
            segment_id=target_row.id,
            target_text="Parágrafo final autônomo.",
            status=schema.SegmentStatus.TRANSLATED,
        )

        repo.update_segment_skeleton(
            project.engine,
            segment_id=target_row.id,
            skeleton=list(target_row.inline_skeleton),
            host_path="/html/body/p[doesnotexist]",
            host_part=target_row.host_part,
            host_total_parts=target_row.host_total_parts,
        )

        # Sanity: the row really has the stale path now, and its
        # translation is intact.
        stale = repo.get_segment(project.engine, target_row.id)
        assert stale is not None
        assert stale.host_path == "/html/body/p[doesnotexist]"
        assert stale.target_text == "Parágrafo final autônomo."

        summary = project.repair_segmentation()

        assert summary.added == 0
        assert summary.rehosted >= 1

        refreshed = repo.get_segment(project.engine, target_row.id)
        assert refreshed is not None
        assert refreshed.host_path != "/html/body/p[doesnotexist]"
        assert refreshed.target_text == "Parágrafo final autônomo."
        assert refreshed.status == schema.SegmentStatus.TRANSLATED
    finally:
        project.close()


def test_repair_event_recorded_per_changed_chapter(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory(chapters=[("Calibre", _CALIBRE_BODY)])
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        chapters = [
            c
            for c in repo.list_chapters(project.engine, project.project_id)
            if c.href != "nav.xhtml"
        ]
        chapter_id = chapters[0].id

        rows = repo.list_segments(project.engine, chapter_id)
        orphan_row = next(r for r in rows if "Stranded paragraph" in r.source_text)
        with project.engine.begin() as conn:
            conn.execute(
                delete(schema.segment).where(schema.segment.c.id == orphan_row.id)
            )

        project.repair_segmentation()

        events = repo.list_events(project.engine, project.project_id)
        repair_events = [e for e in events if e.kind == "chapter.segments_repaired"]
        assert len(repair_events) == 1
        payload = repair_events[0].payload
        assert payload["chapter_id"] == chapter_id
        assert payload["added"] == 1
    finally:
        project.close()


def test_repair_inserted_rows_pick_up_after_max_idx(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """New segments slot in at ``max(existing.idx) + 1``.

    Append-only is the simplest way to keep curator-facing ordering
    stable: existing rows keep their idx (and therefore their place in
    the Reader / Inbox), while recovered orphans show up at the tail
    where the curator can clearly tell they are repair output.
    """

    src = tiny_epub_factory(chapters=[("Calibre", _CALIBRE_BODY)])
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        chapters = [
            c
            for c in repo.list_chapters(project.engine, project.project_id)
            if c.href != "nav.xhtml"
        ]
        chapter_id = chapters[0].id
        rows_before = repo.list_segments(project.engine, chapter_id)
        max_idx_before = max(r.idx for r in rows_before)
        orphan_row = next(
            r for r in rows_before if "Stranded paragraph" in r.source_text
        )

        # Drop the orphan row AND inflate the max idx so the
        # "append after max" branch is exercised on a non-trivial
        # baseline.
        with project.engine.begin() as conn:
            conn.execute(
                delete(schema.segment).where(schema.segment.c.id == orphan_row.id)
            )
            keep = next(r for r in rows_before if r.id != orphan_row.id)
            conn.execute(
                update(schema.segment)
                .where(schema.segment.c.id == keep.id)
                .values(idx=max_idx_before + 99)
            )

        project.repair_segmentation()

        rows_after = repo.list_segments(project.engine, chapter_id)
        new_row = next(r for r in rows_after if "Stranded paragraph" in r.source_text)
        # Must land strictly after the inflated max idx so curator
        # ordering is preserved.
        assert new_row.idx > max_idx_before + 99
    finally:
        project.close()
