"""Pilot tests for the Glossary screen (PRD sections 4.6 / 7.4 / 7.5 / M3)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.main import EpublateApp
from epublate.app.screens.glossary import GlossaryScreen
from epublate.core.project import Project
from epublate.db import repo


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1><p>Élise smiled at Hugo.</p><p>Hugo nodded back.</p>",
            )
        ]
    )
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def _seed_one_entry(project: Project) -> str:
    entry = repo.create_glossary_entry(
        project.engine,
        project_id=project.project_id,
        source_term="Élise",
        target_term="Elisa",
        type="character",
        status="confirmed",
        source_aliases=["Lise"],
    )
    return entry.id


@pytest.mark.asyncio
async def test_glossary_screen_renders_entries(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_one_entry(project)
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, GlossaryScreen)
            assert len(current.entries) == 1
            assert current.entries[0].source_term == "Élise"
    finally:
        project.close()


@pytest.mark.asyncio
async def test_glossary_lock_keystroke_promotes_entry(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        entry_id = _seed_one_entry(project)
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("l")
            await pilot.pause()
            entry = repo.get_glossary_entry(project.engine, entry_id)
            assert entry is not None
            assert entry.status == "locked"
            revisions = repo.list_glossary_revisions(project.engine, entry_id)
            assert len(revisions) == 1
    finally:
        project.close()


@pytest.mark.asyncio
async def test_glossary_filter_cycles_through_statuses(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Élise",
            target_term="Elisa",
            status="locked",
        )
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Hugo",
            target_term="Hugo",
            status="proposed",
        )
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, GlossaryScreen)
            assert current.filter_status == "all"
            await pilot.press("f")
            await pilot.pause()
            assert current.filter_status == "proposed"
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_g_binding_pushes_glossary_screen(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    from epublate.app.screens.reader import ReaderScreen
    from epublate.llm.mock import MockLLMProvider

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("g")
            await pilot.pause()
            assert isinstance(pilot.app.screen, GlossaryScreen)
            # Pop programmatically — the global app-level ``q`` binding is
            # ``priority=True`` (always quits) so screen-level ``q`` can't
            # pop in tests; the assertion that matters is that the screen
            # was pushed.
            pilot.app.pop_screen()
            await pilot.pause()
            assert isinstance(pilot.app.screen, ReaderScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_glossary_cascade_no_op_when_no_segments_match(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Cascade with no affected segments should short-circuit (no modal)."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        # Create an entry whose source/target won't appear anywhere.
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Nonexistent",
            target_term="Inexistente",
            status="confirmed",
        )
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("r")
            await pilot.pause()
            # Still on the GlossaryScreen — cascade was a no-op.
            assert isinstance(pilot.app.screen, GlossaryScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_glossary_merge_duplicates_no_op_when_none(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Pressing ``m`` with no duplicates is a no-op (status update only)."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_one_entry(project)
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
            # Still on the GlossaryScreen — no modal pushed.
            assert isinstance(pilot.app.screen, GlossaryScreen)
            entries = repo.list_glossary_entries(project.engine, project.project_id)
            assert len(entries) == 1
    finally:
        project.close()


@pytest.mark.asyncio
async def test_glossary_merge_duplicates_collapses_groups(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Curator-confirmed merge folds losers into the winner's aliases.

    The auto-proposer used to dedup by ``(source_term, type)`` so the
    same proper noun could land twice. This test exercises the cleanup
    flow on the Glossary screen: press ``m`` to surface the duplicate
    group, ``y`` on the modal to confirm.
    """

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="House",
            target_term="house",
            type="term",
            status="proposed",
        )
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="House",
            target_term="Câmara",
            type="organization",
            status="locked",
        )
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("m")
            await pilot.pause()
            await pilot.press("y")
            await pilot.pause()
            entries = repo.list_glossary_entries(project.engine, project.project_id)
            assert len(entries) == 1
            keep = entries[0]
            # The locked / specific-typed row wins — it sits at the head
            # of the sorted group.
            assert keep.entry.type == "organization"
            assert keep.target_term == "Câmara"
            assert "house" in keep.target_aliases
    finally:
        project.close()


@pytest.mark.asyncio
async def test_glossary_show_occurrences_no_op_when_unused(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Pressing ``o`` on an entry with zero recorded mentions is a no-op."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_one_entry(project)
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            assert isinstance(pilot.app.screen, GlossaryScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_glossary_show_occurrences_lists_recorded_mentions(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The ``o`` action opens the modal with one row per recorded mention.

    Mentions are written by the translation pipeline; this test seeds
    them directly via :func:`repo.record_mentions` so we don't have to
    drive a full translation. The modal must list rows in book order
    (chapter spine_idx → seg.idx → span_start) so the curator can spot
    mis-applied entries fast.
    """

    from epublate.app.screens.glossary import OccurrencesScreen

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        entry_id = _seed_one_entry(project)
        # ``tiny_epub_factory`` lays out a generated NCX/cover chapter
        # before the actual content, so iterate over every chapter
        # and pick the first segment that contains the proper noun.
        chapters = repo.list_chapters(project.engine, project.project_id)
        assert chapters, "tiny_epub_factory must create at least one chapter"
        elise_seg = None
        for chapter in chapters:
            for s in repo.list_segments(project.engine, chapter_id=chapter.id):
                if "Élise" in s.source_text:
                    elise_seg = s
                    break
            if elise_seg is not None:
                break
        assert elise_seg is not None, (
            "tiny_epub_factory must produce an 'Élise' segment somewhere"
        )
        start = elise_seg.source_text.index("Élise")
        repo.record_mentions(
            project.engine,
            segment_id=elise_seg.id,
            mentions=[(entry_id, start, start + len("Élise"))],
        )

        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            assert isinstance(pilot.app.screen, OccurrencesScreen)
            current = pilot.app.screen
            assert isinstance(current, OccurrencesScreen)
            assert len(current._occurrences) == 1
            occ = current._occurrences[0]
            assert occ.segment_id == elise_seg.id
            assert occ.source_span_start == start
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(pilot.app.screen, GlossaryScreen)
    finally:
        project.close()


def test_format_occurrence_snippet_wraps_match_in_guillemets() -> None:
    from epublate.app.screens.glossary import _format_occurrence_snippet

    text = "The Senate met today. The Senate voted yes."
    snippet = _format_occurrence_snippet(text, span_start=4, span_end=10)
    assert "«Senate»" in snippet
    # Whitespace stays single-spaced for one-line table rendering.
    assert "  " not in snippet


def test_format_occurrence_snippet_truncates_long_context() -> None:
    from epublate.app.screens.glossary import _format_occurrence_snippet

    text = "x " * 200 + "Senate" + " y" * 200
    snippet = _format_occurrence_snippet(
        text, span_start=400, span_end=406, max_chars=40
    )
    assert "«Senate»" in snippet
    assert len(snippet) <= 41  # ±1 for the leading/trailing ellipsis or space


def test_format_occurrence_snippet_falls_back_when_span_missing() -> None:
    from epublate.app.screens.glossary import _format_occurrence_snippet

    snippet = _format_occurrence_snippet(
        "  Câmara  e  Câmara  ", span_start=None, span_end=None
    )
    assert snippet == "Câmara e Câmara"


def test_format_occurrence_snippet_strips_placeholders_around_match() -> None:
    """Inline-tag placeholders bleed into surrounding context when the
    match is adjacent to bold/italic spans. They must not reach the
    table cell — both because they're noise to the curator and because
    a stray ``[[/T0]]`` would crash Rich's markup parser."""

    from epublate.app.screens.glossary import _format_occurrence_snippet

    text = "[[T0]]The Senate[[/T0]] met today."
    span_start = text.index("Senate")
    span_end = span_start + len("Senate")
    snippet = _format_occurrence_snippet(text, span_start=span_start, span_end=span_end)
    assert "[[" not in snippet
    assert "[/T" not in snippet
    assert "«Senate»" in snippet


def test_format_occurrence_snippet_escapes_literal_brackets() -> None:
    """A user-authored ``[note]`` chunk in the source must be
    escaped so the table renderer treats it as literal text rather
    than as a Rich tag."""

    from epublate.app.screens.glossary import _format_occurrence_snippet

    text = "Senate met [note] today."
    snippet = _format_occurrence_snippet(text, span_start=0, span_end=6)
    assert "«Senate»" in snippet
    assert r"\[note]" in snippet


@pytest.mark.asyncio
async def test_entry_edit_rejects_asymmetric_particle_pair(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Saving ``Europe → na Europa`` from the modal must fail (PRD F-LB-3).

    The user reported this exact shape produces ``"na na Europa"`` at
    translation time. The modal's symmetry check is the curator-side
    gate: source and target must either both carry a leading
    article/preposition or neither do.
    """

    from textual.widgets import Static

    from epublate.app.screens.glossary import EntryEditScreen, _EntryDraft

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal = EntryEditScreen(
                _EntryDraft(
                    source_term="Europe",
                    target_term="na Europa",
                    type="place",
                    status="proposed",
                    gender=None,
                ),
                title="New entry",
                source_lang=project.source_lang,
                target_lang=project.target_lang,
            )
            pilot.app.push_screen(modal)
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            # The modal stays open and surfaces the asymmetry message.
            assert isinstance(pilot.app.screen, EntryEditScreen)
            err = pilot.app.screen.query_one("#entry-error", Static)
            text = str(err.render())
            assert "na" in text
            assert "Europe" in text
    finally:
        project.close()


@pytest.mark.asyncio
async def test_entry_edit_accepts_balanced_pair(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """``the USA → os EUA`` is symmetric (both have articles) and saves clean."""

    from epublate.app.screens.glossary import EntryEditScreen, _EntryDraft

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal: EntryEditScreen = EntryEditScreen(
                _EntryDraft(
                    source_term="the USA",
                    target_term="os EUA",
                    type="organization",
                    status="proposed",
                    gender="feminine",
                ),
                title="New entry",
                source_lang="en",
                target_lang="pt",
            )
            pilot.app.push_screen(modal)
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            # Modal dismissed → we're back on the GlossaryScreen.
            assert isinstance(pilot.app.screen, GlossaryScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_glossary_edit_modal_mounts_with_blank_gender(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Regression for the ``InvalidSelectValueError: Illegal select value False``
    that surfaced when ``Select.BLANK`` (a Widget-level constant equal to
    ``False``) was passed as the initial value of the gender select for
    an entry with no gender. The modal must use ``Select.NULL`` instead so
    it mounts cleanly for entries without a stored gender (PRD F-LB-3).
    """

    from textual.widgets import Select

    from epublate.app.screens.glossary import EntryEditScreen, _EntryDraft

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            modal: EntryEditScreen = EntryEditScreen(
                _EntryDraft(
                    source_term="Hugo",
                    target_term="Hugo",
                    type="character",
                    status="confirmed",
                    gender=None,
                ),
                title="Edit entry",
            )
            pilot.app.push_screen(modal)
            await pilot.pause()
            assert isinstance(pilot.app.screen, EntryEditScreen)
            gender = pilot.app.screen.query_one("#entry-gender", Select)
            assert gender.value is Select.NULL
            pilot.app.pop_screen()
            await pilot.pause()
    finally:
        project.close()
