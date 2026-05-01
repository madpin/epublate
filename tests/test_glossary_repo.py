"""Tests for glossary CRUD repository helpers (PRD §6.4 / M3)."""

from __future__ import annotations

from sqlalchemy.engine import Engine

from epublate.db import repo


def _ensure_project(engine: Engine, project_id: str = "proj-1") -> str:
    repo.create_project(
        engine,
        name="demo",
        source_lang="en",
        target_lang="pt",
        source_path="/dev/null",
        project_id=project_id,
    )
    return project_id


def test_create_and_get_glossary_entry(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    entry = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Élise",
        target_term="Elisa",
        type="character",
        status="confirmed",
        source_aliases=["Lise", "Lise", "Élise"],
        target_aliases=["Eli"],
    )
    fetched = repo.get_glossary_entry(project_db, entry.id)
    assert fetched is not None
    assert fetched.entry.source_term == "Élise"
    assert fetched.entry.target_term == "Elisa"
    # Aliases are deduped against the canonical term and within each side.
    assert fetched.source_aliases == ["Lise"]
    assert fetched.target_aliases == ["Eli"]


def test_list_glossary_entries_filters_by_status(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    repo.create_glossary_entry(
        project_db, project_id=pid, source_term="A", target_term="A", status="proposed"
    )
    repo.create_glossary_entry(
        project_db, project_id=pid, source_term="B", target_term="B", status="locked"
    )
    repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="C",
        target_term="C",
        status="confirmed",
    )
    locked = repo.list_glossary_entries(project_db, pid, status="locked")
    assert [e.source_term for e in locked] == ["B"]
    all_entries = repo.list_glossary_entries(project_db, pid)
    assert sorted(e.source_term for e in all_entries) == ["A", "B", "C"]


def test_update_records_revision_on_target_change(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    entry = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Élise",
        target_term="Elisa",
        status="confirmed",
    )
    repo.update_glossary_entry(
        project_db,
        entry_id=entry.id,
        target_term="Elise",
        reason="curator-rename",
    )
    revisions = repo.list_glossary_revisions(project_db, entry.id)
    assert len(revisions) == 1
    assert revisions[0].prev_target_term == "Elisa"
    assert revisions[0].new_target_term == "Elise"
    assert revisions[0].reason == "curator-rename"


def test_update_records_revision_on_status_promotion(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    entry = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Élise",
        target_term="Elisa",
        status="confirmed",
    )
    repo.update_glossary_entry(
        project_db,
        entry_id=entry.id,
        status="locked",
    )
    revisions = repo.list_glossary_revisions(project_db, entry.id)
    assert len(revisions) == 1
    assert revisions[0].prev_target_term == "Elisa"
    # No target change → new_target_term mirrors the unchanged term.
    assert revisions[0].new_target_term == "Elisa"


def test_set_aliases_replace_set_semantics(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    entry = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Élise",
        target_term="Elisa",
        source_aliases=["Lise"],
    )
    repo.set_aliases(
        project_db,
        entry_id=entry.id,
        source_aliases=["Lis", "Liz"],
        target_aliases=["Eli"],
    )
    refreshed = repo.get_glossary_entry(project_db, entry.id)
    assert refreshed is not None
    assert refreshed.source_aliases == ["Lis", "Liz"]
    assert refreshed.target_aliases == ["Eli"]


def test_record_mentions_dedupes_and_replaces(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    entry = repo.create_glossary_entry(
        project_db, project_id=pid, source_term="X", target_term="X"
    )
    chap = repo.ChapterRow(
        id="c1",
        project_id=pid,
        spine_idx=0,
        href="c1.xhtml",
        title="C1",
        status="pending",
    )
    repo.bulk_insert_chapters(project_db, [chap])
    seg = repo.SegmentRow(
        id="s1",
        chapter_id="c1",
        idx=0,
        source_text="X did stuff.",
        source_hash="0" * 64,
    )
    repo.bulk_insert_segments(project_db, [seg])
    repo.record_mentions(
        project_db,
        segment_id="s1",
        mentions=[
            (entry.id, 0, 1),
            (entry.id, 0, 1),  # duplicate; should not insert twice
            (entry.id, 5, 6),
        ],
    )
    rows = repo.list_mentions(project_db, segment_id="s1")
    spans = sorted((r.source_span_start, r.source_span_end) for r in rows)
    assert spans == [(0, 1), (5, 6)]

    repo.record_mentions(project_db, segment_id="s1", mentions=[])
    assert repo.list_mentions(project_db, segment_id="s1") == []


def test_find_by_source_term_filters_by_type(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Hope",
        target_term="Esperança",
        type="character",
    )
    repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Hope",
        target_term="esperança",
        type="term",
    )
    char = repo.find_glossary_entry_by_source_term(
        project_db, project_id=pid, source_term="Hope", type="character"
    )
    term = repo.find_glossary_entry_by_source_term(
        project_db, project_id=pid, source_term="Hope", type="term"
    )
    assert char is not None and char.type == "character"
    assert term is not None and term.type == "term"


def test_delete_cascades_aliases(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    entry = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Élise",
        target_term="Elisa",
        source_aliases=["Lise"],
    )
    assert repo.list_aliases(project_db, entry.id)
    repo.delete_glossary_entry(project_db, entry.id)
    assert repo.get_glossary_entry(project_db, entry.id) is None
    assert repo.list_aliases(project_db, entry.id) == []


def test_update_segment_status_does_not_touch_target(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    chap = repo.ChapterRow(
        id="c1",
        project_id=pid,
        spine_idx=0,
        href="x.xhtml",
        title=None,
        status="pending",
    )
    repo.bulk_insert_chapters(project_db, [chap])
    seg = repo.SegmentRow(
        id="s1",
        chapter_id="c1",
        idx=0,
        source_text="hello",
        source_hash="0" * 64,
        target_text="ciao",
        status="translated",
    )
    repo.bulk_insert_segments(project_db, [seg])
    repo.update_segment_status(project_db, segment_id="s1", status="flagged")
    refreshed = repo.get_segment(project_db, "s1")
    assert refreshed is not None
    assert refreshed.status == "flagged"
    assert refreshed.target_text == "ciao"
