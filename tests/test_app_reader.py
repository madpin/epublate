"""Pilot tests for the Reader screen (PRD §4.6, §7.2 / M2)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from textual.widgets import Button

from epublate.app.main import BatchProgress, EpublateApp
from epublate.app.screens.reader import (
    ChapterCard,
    ReaderScreen,
    SegmentCard,
)
from epublate.app.widgets import BatchProgressMeter
from epublate.core.batch import BatchSummary
from epublate.core.project import Project
from epublate.db import repo
from epublate.db.schema import SegmentStatus
from epublate.llm.mock import MockLLMProvider


def _placeholder_safe_responder() -> Callable[..., str]:
    """Mock responder that copies the user prompt into the JSON ``target``."""

    def _responder(messages: list[object], _model: str) -> str:
        # The mock provider passes Message objects in ``messages``.
        user = messages[-1]
        content = getattr(user, "content", "")
        return json.dumps(
            {
                "target": f"PT::{content}",
                "used_entries": [],
                "new_entities": [],
            }
        )

    return _responder


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1><p>Hello, world.</p><p>Second paragraph.</p>",
            )
        ]
    )
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


@pytest.mark.asyncio
async def test_reader_translates_segment_via_t_keystroke(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())

        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            assert isinstance(pilot.app.screen, ReaderScreen)
            await pilot.pause()
            assert provider.call_count == 0

            await pilot.press("t")
            # The translation runs in a worker; let it complete.
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            assert provider.call_count == 1
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            seg = current._current_segment()  # type: ignore[reportPrivateUsage]
            assert seg is not None
            assert seg.target_text is not None
            assert seg.target_text.startswith("PT::")
            assert seg.status == SegmentStatus.TRANSLATED
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_accept_marks_segment_approved(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())

        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.press("t")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()

            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            seg = current._current_segment()  # type: ignore[reportPrivateUsage]
            assert seg is not None
            assert seg.status == SegmentStatus.APPROVED
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_navigates_segments(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            initial_idx = current.segment_idx
            await pilot.press("j")
            await pilot.pause()
            assert current.segment_idx != initial_idx
            await pilot.press("k")
            await pilot.pause()
            assert current.segment_idx == initial_idx
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_retry_bypasses_cache(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.queue_responses(
            [
                json.dumps({"target": "first"}),
                json.dumps({"target": "retry"}),
            ]
        )

        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.press("t")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("r")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert provider.call_count == 2

            # Two translate calls + one cache-bypass salt → both miss.
            calls = repo.list_llm_calls(project.engine, project.project_id)
            translate_calls = [c for c in calls if c.purpose == "translate"]
            assert len(translate_calls) == 2
            assert all(not c.cache_hit for c in translate_calls)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_quit_returns_to_app_idle(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            assert isinstance(pilot.app.screen, ReaderScreen)
            await pilot.press("q")
            # No further screens were pushed; ``q`` quits the app via the
            # global binding when there is nothing to pop.
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_renders_inline_emphasis_with_markup(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Inline tags must render as Rich markup (bold/italic), not as raw
    ``[[T0]]…[[/T0]]`` placeholders that confuse the curator."""

    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1><p>Hello, <em>brave</em> <strong>world</strong>.</p>",
            )
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            # The new chapter view renders every segment as its own
            # SegmentCard inside ``#reader-source-pane``. Walk chapters
            # until we land on one whose mounted cards include the
            # paragraph with the inline pair, then assert on the joined
            # rendered text for that chapter.
            joined = ""
            for _ in range(len(current.state.chapters) + 1):
                joined = ""
                for card in current.query("#reader-source-pane SegmentCard"):
                    assert isinstance(card, SegmentCard)
                    content = card.render()
                    joined += (
                        content.plain if hasattr(content, "plain") else str(content)
                    )
                if "brave" in joined:
                    break
                await pilot.press("n")
                await pilot.pause()
            assert "[[T0]]" not in joined
            assert "[[/T0]]" not in joined
            assert "brave" in joined
            assert "world" in joined
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_arrow_keys_navigate_segments_and_chapters(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Up/Down move segments; Left/Right move chapters."""

    src = tiny_epub_factory(
        chapters=[
            ("Alpha", "<h1>Alpha</h1><p>One.</p><p>Two.</p>"),
            ("Beta", "<h1>Beta</h1><p>Three.</p><p>Four.</p>"),
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            assert len(current.state.chapters) >= 2

            start_chap = current.chapter_idx
            start_seg = current.segment_idx

            await pilot.press("down")
            await pilot.pause()
            assert current.segment_idx != start_seg

            await pilot.press("up")
            await pilot.pause()
            assert current.segment_idx == start_seg

            await pilot.press("right")
            await pilot.pause()
            assert current.chapter_idx == (start_chap + 1) % len(current.state.chapters)

            await pilot.press("left")
            await pilot.pause()
            assert current.chapter_idx == start_chap
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_mouse_click_jumps_to_segment_or_chapter(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Clicking a SegmentCard or ChapterCard updates the reader's cursor."""

    src = tiny_epub_factory(
        chapters=[
            ("Alpha", "<h1>Alpha</h1><p>One.</p><p>Two.</p>"),
            ("Beta", "<h1>Beta</h1><p>Three.</p><p>Four.</p>"),
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            assert len(current.state.chapters) >= 2

            chapter_cards = list(current.query(ChapterCard))
            assert len(chapter_cards) >= 2
            await pilot.click(chapter_cards[1])
            await pilot.pause()
            assert current.chapter_idx == 1

            segment_cards = list(current.query("#reader-source-pane SegmentCard"))
            assert len(segment_cards) >= 2
            target_card = segment_cards[1]
            assert isinstance(target_card, SegmentCard)
            await pilot.click(target_card)
            await pilot.pause()
            current_seg = current._current_segment()  # type: ignore[reportPrivateUsage]
            assert current_seg is not None
            assert current_seg.id == target_card.segment_id
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_shows_translating_cue_during_interactive_translate(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """While ``t`` is in flight, the focused segment cards carry the
    ``-translating`` class and a ``▸ Translating…`` prefix in the body."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            seg = current._current_segment()  # type: ignore[reportPrivateUsage]
            assert seg is not None

            current.action_translate_next()
            # The class is set synchronously before the worker fires.
            source_card = current.query_one(
                f"#reader-source-pane SegmentCard.-status-{seg.status}",
                SegmentCard,
            )
            del source_card  # only used to verify the query path resolves

            translating_cards = list(
                current.query("#reader-source-pane SegmentCard.-translating")
            )
            assert translating_cards, (
                "expected the focused segment to show a -translating cue"
            )
            content = str(translating_cards[0].render())
            assert "Translating" in content

            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert not current.query("#reader-source-pane SegmentCard.-translating")
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_batch_meter_reflects_app_progress(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The Reader's footer meter mirrors the app-level :class:`BatchProgress`.

    Idle: the meter renders ``Batch: idle``. When the Dashboard signals
    that a batch is running on this project, the meter snaps to a live
    snapshot and the ``-active`` class kicks in.
    """

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            meter = current.query_one("#reader-batch-meter", BatchProgressMeter)
            assert "idle" in str(meter.render()).lower()

            running = EpublateApp.__dict__.get("batch_progress")
            assert running is None  # only set per instance, not on class
            pilot.app.batch_progress = BatchProgress(  # type: ignore[attr-defined]
                active=True,
                project_id=project.project_id,
                summary=BatchSummary(
                    translated=2,
                    cached=0,
                    flagged=0,
                    failed=0,
                    cost_usd=0.001,
                    elapsed_s=1.5,
                ),
                total=10,
                paused=False,
            )
            current._tick_live_refresh()  # type: ignore[reportPrivateUsage]
            content = str(meter.render())
            assert "10" in content
            assert "Batch" in content
            assert "idle" not in content.lower()

            pilot.app.batch_progress = BatchProgress(  # type: ignore[attr-defined]
                active=False,
                project_id=None,
                summary=None,
                total=0,
                paused=False,
            )
            current._tick_live_refresh()  # type: ignore[reportPrivateUsage]
            assert "idle" in str(meter.render()).lower()
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_chapter_labels_use_titles_when_present(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """When *some* chapters have titles, the sidebar surfaces those titles
    verbatim and marks the unnamed ones with ``(untitled)`` — never with
    a confusing ``Chapter N`` fallback that fights the leading list
    ordinal."""

    src = tiny_epub_factory(
        chapters=[
            ("Alpha", "<h1>Alpha</h1><p>Alpha body.</p>"),
            # Empty OPF title AND no h1/h2/h3/Calibre heading: this is
            # the chapter that should render as ``(untitled)``.
            ("", "<p>Just body prose, no heading at all.</p>"),
            ("Gamma", "<h1>Gamma</h1><p>Gamma body.</p>"),
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            chapter_cards = list(current.query(ChapterCard))
            rendered = [str(card.render()) for card in chapter_cards]
            joined = "\n".join(rendered)
            assert "Alpha" in joined
            assert "Gamma" in joined
            assert "(untitled)" in joined, rendered
            # Crucially the spine-derived "Chapter N — filename" label
            # must not appear anywhere — that's exactly the dual-numbering
            # collision this test exists to lock down.
            assert "Chapter 2 — " not in joined
            assert "_split_" not in joined
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_chapter_labels_fall_back_to_chapter_n_when_all_untitled(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """If *every* chapter in the project lacks a title (e.g. a bare ePub
    with no h1/h2/h3/Calibre heading and no TOC labels), the sidebar
    falls back to ``Chapter 1``, ``Chapter 2``, … so it still reads as
    a coherent sequence aligned with the leading list ordinal.

    We simulate the all-untitled corner case by creating a normal
    project then nulling every chapter title in the DB. ebooklib's
    auto-generated ``nav.xhtml`` always carries a title (the book
    name), so we can't reach this state purely through the factory.
    """

    from sqlalchemy import update

    from epublate.db import schema

    src = tiny_epub_factory(
        chapters=[
            ("Alpha", "<h1>Alpha</h1><p>Alpha body.</p>"),
            ("Beta", "<h1>Beta</h1><p>Beta body.</p>"),
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        with project.engine.begin() as conn:
            conn.execute(update(schema.chapter).values(title=None))

        provider = MockLLMProvider()
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            assert current.state.all_untitled
            rendered = [str(card.render()) for card in current.query(ChapterCard)]
            joined = "\n".join(rendered)
            assert "Chapter 1" in joined, rendered
            assert "Chapter 2" in joined, rendered
            assert "(untitled)" not in joined, rendered
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_skips_image_only_segments_in_navigation(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Hosts that only contain ``<img>`` placeholderize to ``[[T0]]``;
    the Reader filters them out so the curator never lands on a
    "translate this image" segment (M2 UX)."""

    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1>"
                '<p><img src="cover.png" alt="Cover"/></p>'
                "<p>Real translatable text.</p>",
            )
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            for segs in current.state.segments_by_chapter.values():
                for seg in segs:
                    text = seg.source_text.replace("[[T0]]", "").replace("[[/T0]]", "")
                    assert text.strip(), (
                        "Reader should not show image-only / empty segments"
                    )
    finally:
        project.close()


def _make_two_chapter_project(
    tiny_factory: Callable[..., Path], tmp_path: Path
) -> Project:
    """Two-chapter fixture used by the chapter-batch tests.

    Each chapter has multiple translatable paragraphs so the batch has
    real work to do (an empty pending list completes synchronously and
    bypasses the worker thread, which would defeat the queue test).
    """

    src = tiny_factory(
        chapters=[
            (
                "Alpha",
                "<h1>Alpha</h1>"
                "<p>Alpha first paragraph.</p>"
                "<p>Alpha second paragraph.</p>",
            ),
            (
                "Beta",
                "<h1>Beta</h1>"
                "<p>Beta first paragraph.</p>"
                "<p>Beta second paragraph.</p>",
            ),
        ]
    )
    return Project.create(
        src,
        out_dir=tmp_path / "two-chap-proj",
        source_lang="en",
        target_lang="pt",
    )


@pytest.mark.asyncio
async def test_reader_translate_chapter_via_b_keystroke(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Pressing ``b`` runs ``run_batch`` over every pending segment of
    the current chapter and leaves them in the ``translated`` state."""

    project = _make_two_chapter_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            chap_id = current.state.chapters[current.chapter_idx].id
            initial_segs = current.state.segments_by_chapter[chap_id]
            assert all(s.status == SegmentStatus.PENDING for s in initial_segs)

            await pilot.press("b")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            translated = repo.list_segments(project.engine, chap_id)
            assert translated, "expected segments after batch"
            for seg in translated:
                assert seg.target_text is not None and seg.target_text.startswith(
                    "PT::"
                )
                assert seg.status == SegmentStatus.TRANSLATED

            # The other chapter must remain untouched — the batch was
            # scoped to the active chapter only.
            other_chap = current.state.chapters[1].id
            other_segs = repo.list_segments(project.engine, other_chap)
            assert all(s.status == SegmentStatus.PENDING for s in other_segs), (
                "second chapter should not be touched"
            )
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_translate_chapter_button_click(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Clicking the in-sidebar button mirrors the ``b`` key binding."""

    project = _make_two_chapter_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            button = current.query_one("#reader-translate-chapter", Button)
            await pilot.click(button)
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            chap_id = current.state.chapters[0].id
            translated = repo.list_segments(project.engine, chap_id)
            assert translated
            for seg in translated:
                assert seg.target_text is not None and seg.target_text.startswith(
                    "PT::"
                )
                assert seg.status == SegmentStatus.TRANSLATED
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_chapter_queue_serializes_translations(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A second chapter requested mid-flight queues and runs after the
    first finishes; both queued chapters end fully translated."""

    project = _make_two_chapter_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            assert len(current.state.chapters) >= 2

            chap_a = current.state.chapters[0]
            chap_b = current.state.chapters[1]

            # Two synchronous calls in the same event-loop tick: the
            # first dispatches a worker (sets active chapter), the
            # second sees ``_active_batch_chapter_id`` already set and
            # appends to the queue rather than starting a parallel
            # worker (the contract this test pins down).
            current.chapter_idx = 0
            current.action_translate_chapter()
            current.chapter_idx = 1
            current.action_translate_chapter()
            assert current._chapter_queue == [chap_b.id]  # type: ignore[reportPrivateUsage]
            assert (
                current._active_batch_chapter_id  # type: ignore[reportPrivateUsage]
                == chap_a.id
            )

            # Drain both batches and verify both queued chapters
            # translated. ``wait_for_complete`` returns when all current
            # workers finish; since the second worker is dispatched only
            # *after* the first's BatchFinished message is processed,
            # we need a pause + second wait to drain the queue tail.
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            assert (
                current._active_batch_chapter_id is None  # type: ignore[reportPrivateUsage]
            )
            assert (
                current._chapter_queue == []  # type: ignore[reportPrivateUsage]
            )
            for chap in (chap_a, chap_b):
                segs = current.state.segments_by_chapter.get(chap.id, [])
                assert segs, f"chapter {chap.id!r} unexpectedly empty"
                for seg in segs:
                    assert seg.status == SegmentStatus.TRANSLATED, (
                        f"segment {seg.id} in chapter {chap.id} not translated"
                    )
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_translate_chapter_blocks_on_foreign_batch(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """An active foreign batch (e.g. dispatched from the Dashboard) blocks
    the Reader's chapter queue until it finishes.

    The Reader shares the ``app.batch_progress`` slot with the Dashboard;
    pressing ``b`` while another batch holds the slot should leave the
    Reader's queue untouched and surface a status hint instead of
    starting a competing batch.
    """

    project = _make_two_chapter_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)

            pilot.app.batch_progress = BatchProgress(  # type: ignore[attr-defined]
                active=True,
                project_id=project.project_id,
                summary=None,
                total=10,
                paused=False,
            )
            await pilot.press("b")
            await pilot.pause()

            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            assert (
                current._active_batch_chapter_id is None  # type: ignore[reportPrivateUsage]
            )
            assert provider.call_count == 0
            chap_id = current.state.chapters[0].id
            segs = repo.list_segments(project.engine, chap_id)
            assert all(s.status == SegmentStatus.PENDING for s in segs)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_approve_chapter_via_capital_a(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """``A`` flips every TRANSLATED segment in the current chapter to APPROVED.

    Pending segments stay pending (no target to approve) and other
    chapters remain untouched.
    """

    project = _make_two_chapter_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)

            # Translate the whole chapter first so there's something to
            # approve, then leave segment 0 unapproved on chapter 1
            # (different chapter) to verify scope.
            await pilot.press("b")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)

            chap_a = current.state.chapters[0].id
            chap_b = current.state.chapters[1].id
            assert all(
                s.status == SegmentStatus.TRANSLATED
                for s in repo.list_segments(project.engine, chap_a)
            )

            await pilot.press("A")
            await pilot.pause()

            for seg in repo.list_segments(project.engine, chap_a):
                assert seg.status == SegmentStatus.APPROVED, (
                    f"chapter A segment {seg.id} not approved"
                )
            for seg in repo.list_segments(project.engine, chap_b):
                assert seg.status == SegmentStatus.PENDING, (
                    f"chapter B segment {seg.id} unexpectedly touched"
                )
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_approve_chapter_button_click(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Clicking the Approve-all button mirrors the ``A`` keystroke."""

    project = _make_two_chapter_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)

            await pilot.press("b")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)
            button = current.query_one("#reader-approve-chapter", Button)
            await pilot.click(button)
            await pilot.pause()

            chap_id = current.state.chapters[0].id
            for seg in repo.list_segments(project.engine, chap_id):
                assert seg.status == SegmentStatus.APPROVED
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_approve_chapter_skips_flagged_and_pending(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Approve-all leaves FLAGGED and PENDING segments alone.

    Flagged segments encode a real validator failure (PRD invariant §2);
    pending segments have no target to approve. The action should only
    promote TRANSLATED → APPROVED, and the status line should report
    what was skipped so the curator knows.
    """

    project = _make_two_chapter_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)

            await pilot.press("b")
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)

            chap_a = current.state.chapters[0].id
            translated_segs = repo.list_segments(project.engine, chap_a)
            assert len(translated_segs) >= 2, (
                "fixture must yield at least 2 segments to exercise both buckets"
            )
            # Force one segment to FLAGGED and one back to PENDING so all
            # three buckets are present in the same chapter.
            flagged_id = translated_segs[0].id
            pending_id = translated_segs[1].id
            repo.update_segment_status(
                project.engine,
                segment_id=flagged_id,
                status=SegmentStatus.FLAGGED,
            )
            repo.update_segment_translation(
                project.engine,
                segment_id=pending_id,
                target_text=None,
                status=SegmentStatus.PENDING,
            )

            await pilot.press("A")
            await pilot.pause()

            after = {s.id: s.status for s in repo.list_segments(project.engine, chap_a)}
            assert after[flagged_id] == SegmentStatus.FLAGGED, (
                "flagged segments must not be auto-approved (PRD invariant §2)"
            )
            assert after[pending_id] == SegmentStatus.PENDING, (
                "pending segments have no target to approve"
            )
            for seg_id, status in after.items():
                if seg_id in (flagged_id, pending_id):
                    continue
                assert status == SegmentStatus.APPROVED, (
                    f"translated segment {seg_id} should have been approved"
                )

            status_line = str(
                current.query_one("#reader-status").render()  # type: ignore[arg-type]
            )
            assert "skipped" in status_line.lower()
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_approve_chapter_noop_when_nothing_translated(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Pressing ``A`` on a fully-pending chapter is a clean no-op."""

    project = _make_two_chapter_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)

            await pilot.press("A")
            await pilot.pause()

            chap_id = current.state.chapters[0].id
            for seg in repo.list_segments(project.engine, chap_id):
                assert seg.status == SegmentStatus.PENDING
            assert provider.call_count == 0
    finally:
        project.close()


@pytest.mark.asyncio
async def test_reader_scroll_sync_guard_outlives_synchronous_clear(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Regression: the sync guard must outlive the synchronous return of
    ``scroll_to`` so that any post-layout reactive bounce on the
    destination pane is still suppressed. The earlier implementation
    cleared ``_syncing_scroll`` in a ``finally`` block, which left a
    window for Textual's deferred layout pass to fire the watcher
    *after* the guard was off — every such fire mirrored back to the
    originating pane and the panes flickered non-stop.
    """

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = ReaderScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, ReaderScreen)

            assert current._syncing_scroll is False  # type: ignore[reportPrivateUsage]
            current._begin_syncing_scroll()  # type: ignore[reportPrivateUsage]
            assert current._syncing_scroll is True  # type: ignore[reportPrivateUsage]

            current.call_after_refresh(current._end_syncing_scroll)  # type: ignore[reportPrivateUsage]
            await pilot.pause()
            assert current._syncing_scroll is False, (  # type: ignore[reportPrivateUsage]
                "guard must clear after the next refresh, not synchronously"
            )

            mirror_calls: list[bool] = []
            original_mirror = current._mirror_scroll  # type: ignore[reportPrivateUsage]

            def _spy(*, source_to_target: bool) -> None:
                mirror_calls.append(source_to_target)
                original_mirror(source_to_target=source_to_target)

            current._mirror_scroll = _spy  # type: ignore[method-assign]

            current._begin_syncing_scroll()  # type: ignore[reportPrivateUsage]
            try:
                current._mirror_scroll(source_to_target=True)  # type: ignore[reportPrivateUsage]
                current._mirror_scroll(source_to_target=False)  # type: ignore[reportPrivateUsage]
            finally:
                current._end_syncing_scroll()  # type: ignore[reportPrivateUsage]

            assert mirror_calls == [True, False], (
                "the spy should record entry, but the inner mirror logic "
                "must short-circuit when the guard is set so we never "
                "issue a programmatic scroll on the destination pane"
            )
    finally:
        project.close()
