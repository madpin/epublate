"""Tests for cascade re-translation flow (PRD §7.5 / M3 / F-LB-7)."""

from __future__ import annotations

from sqlalchemy.engine import Engine

from epublate.db import repo
from epublate.glossary import compute_affected
from epublate.glossary.cascade import cascade_retranslate


def _bootstrap(engine: Engine) -> tuple[str, str, str]:
    """Create a project with one chapter and three segments."""

    repo.create_project(
        engine,
        name="demo",
        source_lang="en",
        target_lang="pt",
        source_path="/dev/null",
        project_id="proj-1",
    )
    chap = repo.ChapterRow(
        id="chap-1",
        project_id="proj-1",
        spine_idx=0,
        href="ch1.xhtml",
        title="One",
        status="pending",
    )
    repo.bulk_insert_chapters(engine, [chap])
    segs = [
        repo.SegmentRow(
            id="s1",
            chapter_id="chap-1",
            idx=0,
            source_text="Élise smiled at Hugo.",
            source_hash="0" * 64,
            target_text="Elisa sorriu para Hugo.",
            status="translated",
        ),
        repo.SegmentRow(
            id="s2",
            chapter_id="chap-1",
            idx=1,
            source_text="Hugo nodded.",
            source_hash="1" * 64,
            target_text="Hugo concordou.",
            status="approved",
        ),
        repo.SegmentRow(
            id="s3",
            chapter_id="chap-1",
            idx=2,
            source_text="No relevant names here.",
            source_hash="2" * 64,
            target_text="Nada relevante.",
            status="approved",
        ),
    ]
    repo.bulk_insert_segments(engine, segs)
    return "proj-1", "chap-1", "s1"


def test_compute_affected_picks_up_source_match(project_db: Engine) -> None:
    pid, _, _ = _bootstrap(project_db)
    entry = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Élise",
        target_term="Elisa",
        status="confirmed",
    )
    composite = repo.get_glossary_entry(project_db, entry.id)
    assert composite is not None
    candidates = compute_affected(
        project_db, project_id=pid, entry=composite, prev_target_term=None
    )
    ids = [c.segment_id for c in candidates]
    assert ids == ["s1"]


def test_compute_affected_picks_up_previous_target(project_db: Engine) -> None:
    pid, _, _ = _bootstrap(project_db)
    entry = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Hugo-different",  # source no longer matches s2's source
        target_term="Hugo",
        status="confirmed",
    )
    composite = repo.get_glossary_entry(project_db, entry.id)
    assert composite is not None
    # Previous target was "Hugo" — both s1 and s2 contain "Hugo" in target.
    candidates = compute_affected(
        project_db, project_id=pid, entry=composite, prev_target_term="Hugo"
    )
    ids = sorted(c.segment_id for c in candidates)
    assert ids == ["s1", "s2"]


def test_compute_affected_skips_pending(project_db: Engine) -> None:
    pid, _, _ = _bootstrap(project_db)
    repo.update_segment_status(project_db, segment_id="s1", status="pending")
    entry = repo.create_glossary_entry(
        project_db, project_id=pid, source_term="Élise", target_term="Elisa"
    )
    composite = repo.get_glossary_entry(project_db, entry.id)
    assert composite is not None
    candidates = compute_affected(
        project_db, project_id=pid, entry=composite, prev_target_term=None
    )
    assert candidates == []


def test_cascade_retranslate_reverts_status_and_audits(project_db: Engine) -> None:
    pid, _, _ = _bootstrap(project_db)
    entry = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Élise",
        target_term="Elise",
        status="locked",
    )
    composite = repo.get_glossary_entry(project_db, entry.id)
    assert composite is not None
    candidates = compute_affected(
        project_db,
        project_id=pid,
        entry=composite,
        prev_target_term="Elisa",  # the old translation we want to invalidate
    )
    assert {c.segment_id for c in candidates} == {"s1"}

    n = cascade_retranslate(
        project_db,
        project_id=pid,
        entry=composite,
        prev_target_term="Elisa",
        new_target_term="Elise",
        candidates=candidates,
        reason="rename",
    )
    assert n == 1

    refreshed = repo.get_segment(project_db, "s1")
    assert refreshed is not None
    assert refreshed.status == "pending"
    assert refreshed.target_text is None

    events = repo.list_events(project_db, pid)
    seg_events = [e for e in events if e.kind == "segment.cascaded"]
    glossary_events = [e for e in events if e.kind == "glossary.cascaded"]
    assert len(seg_events) == 1
    # Prior text preserved in the audit payload (PRD §7.5: history first).
    assert seg_events[0].payload["prior_target_text"] == "Elisa sorriu para Hugo."
    assert glossary_events[0].payload["affected_count"] == 1
