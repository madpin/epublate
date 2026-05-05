"""Tests for the new intake-run + project-context-defaults repo helpers.

Both feature surfaces share one fixture file so the repo-level
contract is exercised in one place; the higher-level extractor
wiring + Settings dispatch each have their own dedicated tests.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine

from epublate.db import repo
from epublate.db.schema import IntakeRunKind, IntakeRunStatus


def _make_project(engine: Engine, *, name: str = "demo") -> repo.ProjectRow:
    return repo.create_project(
        engine,
        name=name,
        source_lang="en",
        target_lang="pt",
        source_path="/tmp/x.epub",
    )


def _make_chapter(engine: Engine, project_id: str, *, idx: int = 0) -> repo.ChapterRow:
    chap = repo.ChapterRow(
        id=f"chap-{idx:02d}",
        project_id=project_id,
        spine_idx=idx,
        href=f"chap-{idx:02d}.xhtml",
        title=f"Chapter {idx + 1}",
    )
    repo.bulk_insert_chapters(engine, [chap])
    return chap


def _make_glossary_entry(engine: Engine, project_id: str, *, source_term: str) -> str:
    """Create a project-scoped proposed glossary entry, return its id."""

    entry = repo.create_glossary_entry(
        engine,
        project_id=project_id,
        type="character",
        source_term=source_term,
        target_term=source_term + "-pt",
        status="proposed",
    )
    return entry.id


def test_record_intake_run_persists_full_payload(project_db: Engine) -> None:
    project = _make_project(project_db)
    chapter = _make_chapter(project_db, project.id)

    row = repo.record_intake_run(
        project_db,
        project_id=project.id,
        kind=IntakeRunKind.CHAPTER_PRE_PASS,
        chapter_id=chapter.id,
        helper_model="gpt-helper",
        started_at=1_700_000_000,
        finished_at=1_700_000_042,
        status=IntakeRunStatus.COMPLETED,
        chunks=3,
        cached_chunks=1,
        proposed_count=2,
        failed_chunks=0,
        prompt_tokens=120,
        completion_tokens=80,
        cost_usd=0.0042,
        pov="third",
        tense="past",
        narrative_register="literary",
        audience="adult",
        suggested_style_profile="literary_fiction",
        notes=["watch out for honorifics", "lots of dialogue"],
    )
    assert row.kind == IntakeRunKind.CHAPTER_PRE_PASS
    assert row.status == IntakeRunStatus.COMPLETED
    assert row.chapter_id == chapter.id
    assert row.notes == ["watch out for honorifics", "lots of dialogue"]
    assert row.cost_usd == 0.0042

    fetched = repo.get_intake_run(project_db, row.id)
    assert fetched is not None
    assert fetched.helper_model == "gpt-helper"
    assert fetched.narrative_register == "literary"
    assert fetched.notes == ["watch out for honorifics", "lots of dialogue"]


def test_record_intake_run_rejects_unknown_kind(project_db: Engine) -> None:
    project = _make_project(project_db)
    try:
        repo.record_intake_run(
            project_db,
            project_id=project.id,
            kind="not_a_kind",
            helper_model="m",
            started_at=0,
            finished_at=0,
            status=IntakeRunStatus.COMPLETED,
        )
    except ValueError as exc:
        assert "intake_run kind" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for unknown kind")


def test_attach_intake_run_entries_links_proposed(project_db: Engine) -> None:
    project = _make_project(project_db)
    e1 = _make_glossary_entry(project_db, project.id, source_term="Alice")
    e2 = _make_glossary_entry(project_db, project.id, source_term="Bob")

    row = repo.record_intake_run(
        project_db,
        project_id=project.id,
        kind=IntakeRunKind.BOOK_INTAKE,
        helper_model="m",
        started_at=0,
        finished_at=1,
        status=IntakeRunStatus.COMPLETED,
    )
    inserted = repo.attach_intake_run_entries(
        project_db, intake_run_id=row.id, entry_ids=[e1, e2]
    )
    assert inserted == 2

    # Idempotent: re-linking the same entries adds nothing.
    again = repo.attach_intake_run_entries(
        project_db, intake_run_id=row.id, entry_ids=[e1, e2]
    )
    assert again == 0

    fetched = repo.get_intake_run(project_db, row.id)
    assert fetched is not None
    assert sorted(fetched.proposed_entry_ids) == sorted([e1, e2])


def test_list_intake_runs_returns_newest_first_with_links(
    project_db: Engine,
) -> None:
    project = _make_project(project_db)
    e1 = _make_glossary_entry(project_db, project.id, source_term="X")

    older = repo.record_intake_run(
        project_db,
        project_id=project.id,
        kind=IntakeRunKind.BOOK_INTAKE,
        helper_model="m",
        started_at=1_700_000_000,
        finished_at=1_700_000_005,
        status=IntakeRunStatus.COMPLETED,
    )
    newer = repo.record_intake_run(
        project_db,
        project_id=project.id,
        kind=IntakeRunKind.CHAPTER_PRE_PASS,
        helper_model="m",
        started_at=1_700_001_000,
        finished_at=1_700_001_005,
        status=IntakeRunStatus.COMPLETED,
    )
    repo.attach_intake_run_entries(project_db, intake_run_id=newer.id, entry_ids=[e1])

    rows = repo.list_intake_runs(project_db, project_id=project.id)
    assert [r.id for r in rows] == [newer.id, older.id]
    # Each row carries its own proposed_entry_ids from the join table.
    assert rows[0].proposed_entry_ids == [e1]
    assert rows[1].proposed_entry_ids == []

    # ``kind`` filter narrows correctly.
    only_chapters = repo.list_intake_runs(
        project_db, project_id=project.id, kind=IntakeRunKind.CHAPTER_PRE_PASS
    )
    assert [r.id for r in only_chapters] == [newer.id]


def test_update_intake_run_curator_notes_round_trip(project_db: Engine) -> None:
    project = _make_project(project_db)
    row = repo.record_intake_run(
        project_db,
        project_id=project.id,
        kind=IntakeRunKind.BOOK_INTAKE,
        helper_model="m",
        started_at=0,
        finished_at=1,
        status=IntakeRunStatus.COMPLETED,
    )
    edited = repo.update_intake_run_curator_notes(
        project_db, intake_run_id=row.id, curator_notes="rejected this run's POV"
    )
    assert edited.curator_notes == "rejected this run's POV"

    cleared = repo.update_intake_run_curator_notes(
        project_db, intake_run_id=row.id, curator_notes="   "
    )
    assert cleared.curator_notes is None


def test_update_project_context_defaults_persists_and_emits_event(
    project_db: Engine,
) -> None:
    project = _make_project(project_db)
    refreshed = repo.update_project_context_defaults(
        project_db,
        project_id=project.id,
        max_segments=4,
        max_chars=1200,
    )
    assert refreshed.context_max_segments == 4
    assert refreshed.context_max_chars == 1200

    fetched = repo.get_project(project_db, project.id)
    assert fetched is not None
    assert fetched.context_max_segments == 4
    assert fetched.context_max_chars == 1200

    events = [
        e
        for e in repo.list_events(project_db, project.id)
        if e.kind == "project.context_defaults_changed"
    ]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["prev_max_segments"] == 0
    assert payload["new_max_segments"] == 4
    assert payload["prev_max_chars"] == 0
    assert payload["new_max_chars"] == 1200


def test_update_project_context_defaults_rejects_negative(
    project_db: Engine,
) -> None:
    project = _make_project(project_db)
    try:
        repo.update_project_context_defaults(
            project_db,
            project_id=project.id,
            max_segments=-1,
            max_chars=0,
        )
    except ValueError as exc:
        assert "max_segments" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError")


def test_create_project_defaults_context_to_zero(project_db: Engine) -> None:
    project = _make_project(project_db)
    fetched = repo.get_project(project_db, project.id)
    assert fetched is not None
    assert fetched.context_max_segments == 0
    assert fetched.context_max_chars == 0
