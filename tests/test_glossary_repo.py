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


def test_find_duplicate_source_terms_groups_by_source(project_db: Engine) -> None:
    """Surface every group of entries that share a source term.

    Returned groups are sorted with the "most useful winner" first
    (locked > confirmed > proposed; specific type before generic
    ``term``) so the curator can accept the head as-is.
    """

    pid = _ensure_project(project_db)
    repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="House",
        target_term="house",
        type="term",
        status="proposed",
    )
    repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="House",
        target_term="Câmara",
        type="organization",
        status="locked",
    )
    repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Lone",
        target_term="Sozinho",
        type="character",
    )
    groups = repo.find_duplicate_source_terms(project_db, pid)
    assert len(groups) == 1
    sources = [e.source_term for e in groups[0]]
    types = [e.entry.type for e in groups[0]]
    assert sources == ["House", "House"]
    assert types[0] == "organization"
    assert types[1] == "term"


def test_find_duplicate_source_terms_skips_target_only(project_db: Engine) -> None:
    """Target-only Lore Book entries (``source_term IS NULL``) are not duplicates."""

    pid = _ensure_project(project_db)
    repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term=None,
        target_term="Geralt de Rívia",
        type="character",
        source_known=False,
    )
    repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term=None,
        target_term="Yennefer de Vengerberg",
        type="character",
        source_known=False,
    )
    assert repo.find_duplicate_source_terms(project_db, pid) == []


def test_merge_glossary_entries_folds_aliases_and_mentions(project_db: Engine) -> None:
    """Merge keeps the winner intact and absorbs the losers' aliases + mentions.

    After the merge, ``entity_mention`` rows that pointed at any
    loser must now point at the winner so the audit trail is
    preserved (glossary-invariants §5: "no silent merges"). A
    ``glossary_revision`` row is appended on the winner so the
    history surfaces in the Glossary detail pane.
    """

    pid = _ensure_project(project_db)
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
        source_text="House",
        source_hash="0" * 64,
    )
    repo.bulk_insert_segments(project_db, [seg])

    winner = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="House",
        target_term="Câmara",
        type="organization",
        status="locked",
        target_aliases=["Casa"],
    )
    loser = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="House",
        target_term="house",
        type="term",
        status="proposed",
        source_aliases=["the House"],
    )
    repo.record_mentions(
        project_db,
        segment_id="s1",
        mentions=[(loser.id, 0, 5)],
    )

    removed = repo.merge_glossary_entries(
        project_db,
        winner_id=winner.id,
        loser_ids=[loser.id],
        reason="curator:merge",
    )
    assert removed == 1
    assert repo.get_glossary_entry(project_db, loser.id) is None
    refreshed = repo.get_glossary_entry(project_db, winner.id)
    assert refreshed is not None
    assert refreshed.target_term == "Câmara"
    assert "the House" in refreshed.source_aliases
    assert "house" in refreshed.target_aliases
    # Mentions should now point at the winner so the segment's
    # history isn't lost.
    mentions = repo.list_mentions(project_db, entry_id=winner.id)
    assert any(m.segment_id == "s1" for m in mentions)
    revisions = repo.list_glossary_revisions(project_db, winner.id)
    assert any(r.reason == "curator:merge" for r in revisions)


def test_merge_glossary_entries_no_op_on_empty_losers(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    winner = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="A",
        target_term="A",
    )
    merged = repo.merge_glossary_entries(project_db, winner_id=winner.id, loser_ids=[])
    assert merged == 0


def test_count_mentions_per_entry_aggregates_total_and_segments(
    project_db: Engine,
) -> None:
    """Aggregate counts: total mentions and distinct segments per entry.

    Several mentions in the same segment count toward ``mentions``
    once per match, but only once toward ``segments`` — that's the
    contract surfaced as ``"5 (3s)"`` on the Glossary screen.
    """

    pid = _ensure_project(project_db)
    other = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Other",
        target_term="Outro",
    )
    senate = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Senate",
        target_term="Senado",
    )
    chap = repo.ChapterRow(
        id="c1", project_id=pid, spine_idx=0, href="c.xhtml", status="pending"
    )
    repo.bulk_insert_chapters(project_db, [chap])
    segs = [
        repo.SegmentRow(
            id="s1",
            chapter_id="c1",
            idx=0,
            source_text="Senate met. Senate voted.",
            source_hash="0" * 64,
        ),
        repo.SegmentRow(
            id="s2",
            chapter_id="c1",
            idx=1,
            source_text="Senate adjourned.",
            source_hash="1" * 64,
        ),
    ]
    repo.bulk_insert_segments(project_db, segs)
    repo.record_mentions(
        project_db,
        segment_id="s1",
        mentions=[(senate.id, 0, 6), (senate.id, 12, 18)],
    )
    repo.record_mentions(
        project_db,
        segment_id="s2",
        mentions=[(senate.id, 0, 6)],
    )

    counts = repo.count_mentions_per_entry(project_db, pid)
    assert counts[senate.id] == repo.MentionCounts(mentions=3, segments=2)
    assert other.id not in counts


def test_count_mentions_per_entry_scoped_to_project(project_db: Engine) -> None:
    """A sibling project's mentions never leak into another project's counts."""

    pid = _ensure_project(project_db, project_id="proj-A")
    other_pid = _ensure_project(project_db, project_id="proj-B")
    entry = repo.create_glossary_entry(
        project_db, project_id=pid, source_term="X", target_term="X"
    )
    repo.bulk_insert_chapters(
        project_db,
        [
            repo.ChapterRow(
                id="cA", project_id=pid, spine_idx=0, href="a.xhtml", status="pending"
            ),
            repo.ChapterRow(
                id="cB",
                project_id=other_pid,
                spine_idx=0,
                href="b.xhtml",
                status="pending",
            ),
        ],
    )
    repo.bulk_insert_segments(
        project_db,
        [
            repo.SegmentRow(
                id="sA",
                chapter_id="cA",
                idx=0,
                source_text="X",
                source_hash="0" * 64,
            ),
            repo.SegmentRow(
                id="sB",
                chapter_id="cB",
                idx=0,
                source_text="X",
                source_hash="1" * 64,
            ),
        ],
    )
    repo.record_mentions(project_db, segment_id="sA", mentions=[(entry.id, 0, 1)])
    repo.record_mentions(project_db, segment_id="sB", mentions=[(entry.id, 0, 1)])

    counts_a = repo.count_mentions_per_entry(project_db, pid)
    counts_b = repo.count_mentions_per_entry(project_db, other_pid)
    assert counts_a[entry.id].mentions == 1
    assert counts_a[entry.id].segments == 1
    assert counts_b[entry.id].mentions == 1


def test_list_occurrences_orders_by_book_position(project_db: Engine) -> None:
    """Occurrences come back in book order: spine_idx → seg.idx → span_start.

    The Glossary "Show occurrences" modal feeds rows straight into a
    DataTable, so the repo must do the ordering — otherwise the
    curator sees a chaotic, insertion-order list.
    """

    pid = _ensure_project(project_db)
    entry = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="House",
        target_term="Câmara",
    )
    chapters = [
        repo.ChapterRow(
            id="c2",
            project_id=pid,
            spine_idx=1,
            href="c2.xhtml",
            title="Two",
            status="pending",
        ),
        repo.ChapterRow(
            id="c1",
            project_id=pid,
            spine_idx=0,
            href="c1.xhtml",
            title="One",
            status="pending",
        ),
    ]
    repo.bulk_insert_chapters(project_db, chapters)
    segs = [
        repo.SegmentRow(
            id="s2-1",
            chapter_id="c2",
            idx=1,
            source_text="The House debated.",
            source_hash="2" * 64,
            target_text="A Câmara debateu.",
        ),
        repo.SegmentRow(
            id="s1-0",
            chapter_id="c1",
            idx=0,
            source_text="House and House.",
            source_hash="1" * 64,
            target_text="Câmara e Câmara.",
        ),
    ]
    repo.bulk_insert_segments(project_db, segs)
    # Insert mentions out of order to verify the sort happens in SQL.
    repo.record_mentions(
        project_db,
        segment_id="s2-1",
        mentions=[(entry.id, 4, 9)],
    )
    repo.record_mentions(
        project_db,
        segment_id="s1-0",
        mentions=[(entry.id, 11, 16), (entry.id, 0, 5)],
    )

    rows = repo.list_occurrences(project_db, project_id=pid, entry_id=entry.id)
    keyed = [(r.chapter_spine_idx, r.segment_idx, r.source_span_start) for r in rows]
    assert keyed == [(0, 0, 0), (0, 0, 11), (1, 1, 4)]
    assert rows[0].chapter_title == "One"
    assert rows[2].chapter_title == "Two"
    assert rows[0].target_text == "Câmara e Câmara."


def test_list_occurrences_empty_for_unknown_entry(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    assert repo.list_occurrences(project_db, project_id=pid, entry_id="nope") == []


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
