"""Reader screen — interactive translation workspace (PRD §4.6, §7.2).

The Reader is the curator's primary working surface: a three-column
chapter-aware view that surfaces enough context to translate one
segment at a time without losing the surrounding paragraphs.

Layout (left → right):

* **Chapter sidebar.** Read-only TOC with the current chapter
  highlighted plus a per-chapter completion glyph. ``left`` /
  ``right`` arrows (or ``n`` / ``p``) jump between chapters; clicking
  a row jumps directly. ``J`` / ``K`` are kept as power-user aliases.
* **Source pane.** Every segment of the current chapter rendered
  as its own widget. The focused segment is highlighted; the rest
  fade so the curator can read the chapter as prose.
* **Target pane.** Mirror of the source pane showing translations
  (or a faded "(not yet translated)" placeholder).

UX hooks the curator depends on:

* **Mouse-clickable** chapter rows and segment cards on either side.
* **Visible cue** while a segment is mid-translate: the focused card
  picks up a warning border + a ``▸ Translating…`` prefix until the
  worker posts its outcome.
* **Live refresh** every 1.5 s while a batch is running so the
  Reader doubles as a live progress monitor — segments flip from
  ``pending`` to ``translated`` in place and a footer line
  summarizes the batch.

All long operations run in Textual workers so the UI never blocks
(TUI rule §1).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

from rich.markup import escape
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.timer import Timer
from textual.widgets import Button, Footer, Header, Label, Static, TextArea

from epublate.app.preview import has_translatable_text, render_preview
from epublate.app.widgets import BatchProgressMeter, BatchSnapshot
from epublate.core.batch import (
    BatchOptions,
    BatchPaused,
    BatchProgressEvent,
    BatchSummary,
    run_batch,
)
from epublate.core.pipeline import (
    TranslateOptions,
    TranslateOutcome,
    translate_segment,
)
from epublate.core.project import Project
from epublate.db import repo
from epublate.db.schema import SegmentStatus
from epublate.formats.base import InlineToken
from epublate.llm.base import LLMProvider
from epublate.llm.factory import build_provider, resolve_helper_model

if TYPE_CHECKING:
    from epublate.app.main import BatchProgress

DEFAULT_MODEL = "gpt-5-mini"

ProviderFactory = Callable[[], LLMProvider]

# Status → glyph used in both the chapter sidebar and the segment
# margin so the curator can read progress at a glance without
# squinting at colored text.
_STATUS_GLYPH: dict[str, str] = {
    SegmentStatus.PENDING: "·",
    SegmentStatus.TRANSLATED: "○",
    SegmentStatus.APPROVED: "●",
    SegmentStatus.FLAGGED: "!",
}

_LIVE_REFRESH_SECONDS = 1.5


@dataclass(slots=True)
class _ReaderState:
    """Snapshot of the project's chapters + segments for the Reader.

    Built once on mount and refreshed lazily after writes; the Reader
    intentionally avoids polling the DB (the only periodic refresh
    happens during batch runs, see :meth:`_tick_live_refresh`).
    """

    chapters: list[repo.ChapterRow]
    segments_by_chapter: dict[str, list[repo.SegmentRow]]

    @property
    def all_untitled(self) -> bool:
        """True if no chapter in the project carries a real title.

        Drives the sidebar's labelling fallback: with at least one
        titled chapter the unnamed ones get an explicit ``(untitled)``
        marker; with none, the list-position ordinal becomes the
        primary label so the sidebar reads ``Chapter 1``, ``Chapter 2``
        in sequence (PRD §4.6 Reader UX).
        """

        return not any(c.title for c in self.chapters)


class PipelineFinished(Message):
    """Posted by the translate-worker once a single-segment call returns."""

    def __init__(self, outcome: TranslateOutcome) -> None:
        super().__init__()
        self.outcome = outcome


class PipelineFailed(Message):
    """Posted by the translate-worker on a typed error."""

    def __init__(self, error: str, *, segment_id: str | None) -> None:
        super().__init__()
        self.error = error
        self.segment_id = segment_id


class SegmentClicked(Message):
    """Mouse-click on a :class:`SegmentCard`."""

    def __init__(self, segment_id: str) -> None:
        super().__init__()
        self.segment_id = segment_id


class ChapterClicked(Message):
    """Mouse-click on a :class:`ChapterCard`."""

    def __init__(self, chapter_idx: int) -> None:
        super().__init__()
        self.chapter_idx = chapter_idx


class ChapterBatchTick(Message):
    """One per-segment tick from the reader's chapter-batch worker."""

    def __init__(self, event: BatchProgressEvent) -> None:
        super().__init__()
        self.event = event


class ChapterBatchFinished(Message):
    """The chapter-batch worker drained the chapter (clean or paused)."""

    def __init__(self, summary: BatchSummary, *, paused: bool, chapter_id: str) -> None:
        super().__init__()
        self.summary = summary
        self.paused = paused
        self.chapter_id = chapter_id


class ChapterBatchFailed(Message):
    """The chapter-batch worker raised before completion."""

    def __init__(self, error: str, *, chapter_id: str) -> None:
        super().__init__()
        self.error = error
        self.chapter_id = chapter_id


class EditTargetScreen(ModalScreen[str | None]):
    """Modal to edit a target segment in place (PRD §7.2 'edit and accept').

    Public so Textual's CSS parser sees a normal class name. Not part of
    the package's external API (kept out of ``__all__``).
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    EditTargetScreen {
        align: center middle;
    }
    EditTargetScreen #edit-box {
        width: 80%;
        height: 60%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    EditTargetScreen #edit-help {
        height: 1;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        source: str,
        target: str,
        skeleton: list[InlineToken] | None = None,
    ) -> None:
        super().__init__()
        self._source = source
        self._target = target
        self._skeleton: list[InlineToken] = list(skeleton or [])

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-box"):
            yield Label("Source:", classes="muted")
            yield Static(
                render_preview(self._source, self._skeleton) or self._source,
                markup=True,
            )
            yield Label("Target (Ctrl+S to save, Escape to cancel):")
            yield TextArea(self._target, id="edit-target")
            yield Static(
                "Tip: keep [[T0]]…[[/T0]] placeholders intact.",
                id="edit-help",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#edit-target", TextArea).focus()

    def action_save(self) -> None:
        text = self.query_one("#edit-target", TextArea).text
        self.dismiss(text)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SegmentCard(Static):
    """One segment rendered as its own widget for highlighting + scrolling.

    The card carries its segment id so the Reader can look the matching
    DOM widget up without rebuilding the panes on every cursor move. CSS
    classes encode the focus / status / translating state — colors
    come from the active Textual theme so the card respects light /
    dark / contrast modes. Mouse clicks bubble a :class:`SegmentClicked`
    message for the parent screen to handle.
    """

    DEFAULT_CSS = """
    SegmentCard {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        border-left: thick transparent;
    }
    SegmentCard.-status-pending {
        color: $text-muted;
    }
    SegmentCard.-status-translated {
        color: $text;
    }
    SegmentCard.-status-approved {
        color: $success;
        text-style: bold;
    }
    SegmentCard.-status-flagged {
        color: $warning;
    }
    SegmentCard.-current {
        background: $boost;
        border-left: thick $accent;
    }
    SegmentCard.-translating {
        background: $warning 30%;
        border-left: thick $warning;
    }
    SegmentCard:hover {
        background: $boost;
    }
    """

    def __init__(self, *, segment_id: str, status: str, content: str) -> None:
        super().__init__(content, markup=True)
        self._segment_id = segment_id
        self._status = status
        self.add_class(f"-status-{status}")

    @property
    def segment_id(self) -> str:
        return self._segment_id

    @property
    def status(self) -> str:
        return self._status

    def update_status(self, status: str) -> None:
        if status == self._status:
            return
        self.remove_class(f"-status-{self._status}")
        self.add_class(f"-status-{status}")
        self._status = status

    def update_content(self, content: str) -> None:
        self.update(content)

    def set_current(self, current: bool) -> None:
        if current:
            self.add_class("-current")
        else:
            self.remove_class("-current")

    def set_translating(self, translating: bool) -> None:
        if translating:
            self.add_class("-translating")
        else:
            self.remove_class("-translating")

    def on_click(self) -> None:
        self.post_message(SegmentClicked(self._segment_id))


class ChapterCard(Static):
    """Sidebar row for one chapter, clickable + status-aware.

    Renders the chapter ordinal, title, and an "approved/total"
    indicator. Posts a :class:`ChapterClicked` message when the
    curator taps it.
    """

    DEFAULT_CSS = """
    ChapterCard {
        height: auto;
        padding: 0 1;
        margin: 0;
    }
    ChapterCard:hover {
        background: $boost;
    }
    ChapterCard.-current {
        background: $boost;
        text-style: bold;
        color: $accent;
    }
    """

    def __init__(self, *, chapter_idx: int, content: str) -> None:
        super().__init__(content, markup=True)
        self._chapter_idx = chapter_idx

    @property
    def chapter_idx(self) -> int:
        return self._chapter_idx

    def update_content(self, content: str) -> None:
        self.update(content)

    def set_current(self, current: bool) -> None:
        if current:
            self.add_class("-current")
        else:
            self.remove_class("-current")

    def on_click(self) -> None:
        self.post_message(ChapterClicked(self._chapter_idx))


class ReaderScreen(Screen[None]):
    """Three-column reader for one project (PRD §4.6, §7.2)."""

    # ``priority=True`` on the arrow-key bindings intercepts the
    # default ``VerticalScroll`` scroll behavior so the panes always
    # obey the Reader's segment/chapter navigation contract instead of
    # scrolling line-by-line. Users can still scroll with PageUp/PageDown
    # or the mouse wheel; the Reader handles scroll-into-view on
    # selection.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("t", "translate_next", "Translate", show=True),
        Binding("b", "translate_chapter", "Translate chapter", show=True),
        Binding("r", "retry", "Retry", show=True),
        Binding("a", "accept", "Accept", show=True),
        Binding("A", "approve_chapter", "Approve chapter", show=True),
        Binding("e", "edit", "Edit", show=True),
        Binding("g", "open_glossary", "Glossary", show=True),
        Binding("down", "next_segment", "↓ seg", show=True, priority=True),
        Binding("up", "prev_segment", "↑ seg", show=True, priority=True),
        Binding("right", "next_chapter", "→ chap", show=True, priority=True),
        Binding("left", "prev_chapter", "← chap", show=True, priority=True),
        Binding("j", "next_segment", "Next seg", show=False),
        Binding("k", "prev_segment", "Prev seg", show=False),
        Binding("n,]", "next_chapter", "Next chap", show=False),
        Binding("p,[", "prev_chapter", "Prev chap", show=False),
        Binding("J", "next_chapter", "Next chap", show=False),
        Binding("K", "prev_chapter", "Prev chap", show=False),
        Binding("G,end", "last_segment", "End", show=False, priority=True),
        Binding("ctrl+home,home", "first_segment", "Top", show=False, priority=True),
        Binding("q", "app.pop_screen", "Back", show=True),
        # ``escape`` mirrors ``q`` so curators can back out with the
        # universal "cancel" key; hidden from the footer to keep the
        # already-busy binding bar concise.
        Binding("escape", "app.pop_screen", "Back", show=False),
    ]

    DEFAULT_CSS = """
    ReaderScreen #reader-body {
        height: 1fr;
    }
    ReaderScreen #reader-chapters-panel {
        width: 32;
        max-width: 40%;
        height: 1fr;
    }
    ReaderScreen #reader-chapter-actions {
        height: auto;
        padding: 0;
    }
    ReaderScreen #reader-translate-chapter,
    ReaderScreen #reader-approve-chapter {
        width: 1fr;
        margin: 0;
    }
    ReaderScreen #reader-chapters {
        height: 1fr;
        border: round $primary;
        padding: 0 1;
    }
    ReaderScreen #reader-chapters > .-empty {
        color: $text-muted;
    }
    ReaderScreen .reader-pane {
        width: 1fr;
        height: 1fr;
        padding: 1 1;
        border: round $primary;
    }
    ReaderScreen .reader-pane-empty {
        color: $text-muted;
    }
    ReaderScreen #reader-status-bar {
        dock: bottom;
        height: auto;
        background: $boost;
    }
    ReaderScreen #reader-status {
        height: 1;
        padding: 0 1;
    }
    ReaderScreen BatchProgressMeter {
        height: auto;
        padding: 0 1;
    }
    ReaderScreen BatchProgressMeter.-active {
        background: $boost;
    }
    """

    chapter_idx: reactive[int] = reactive(0)
    segment_idx: reactive[int] = reactive(0)

    def __init__(
        self,
        project: Project,
        *,
        provider_factory: ProviderFactory = build_provider,
        model: str = DEFAULT_MODEL,
        initial_chapter_id: str | None = None,
    ) -> None:
        super().__init__()
        self._project = project
        self._provider_factory = provider_factory
        self._model = model
        self._provider: LLMProvider | None = None
        # ``initial_chapter_id`` lets the Dashboard's chapter table jump
        # straight to a chapter on Enter. We resolve it to the
        # translatable index in :meth:`_load_state` because we don't
        # know the chapter list until SQL has been read; until then we
        # stash the requested id so the resolver can't be skipped.
        self._initial_chapter_id = initial_chapter_id
        self._state: _ReaderState = _ReaderState(chapters=[], segments_by_chapter={})
        # Segment id → SegmentCard for the source / target panes. Both
        # panes mount/unmount in lockstep when the chapter changes.
        self._source_cards: dict[str, SegmentCard] = {}
        self._target_cards: dict[str, SegmentCard] = {}
        self._chapter_cards: list[ChapterCard] = []
        self._translating_segment_ids: set[str] = set()
        self._refresh_timer: Timer | None = None
        self._batch_meter_visible = False
        # Chapter-batch queue state. The Reader serializes chapter
        # translations one at a time so each call's progress meter is
        # interpretable; a second 'b' press while a chapter is mid-flight
        # appends to ``_chapter_queue`` and starts after the current one
        # completes (PRD §4.6 "Reader doubles as a live progress monitor").
        self._chapter_queue: list[str] = []
        self._active_batch_chapter_id: str | None = None
        self._active_batch_total: int = 0
        # Set while we drive a programmatic scroll on either pane so the
        # source<->target sync watcher doesn't recurse on itself.
        # ``_render_segment_panes`` and ``_highlight_current_segment``
        # also flip this on while they re-anchor the panes after a chapter
        # change so the watchers see the new layout as a single move.
        # We clear via ``call_after_refresh`` rather than synchronously:
        # ``scroll_to`` writes ``scroll_y`` immediately but the
        # destination pane's *layout* (and any post-clamp reactive
        # bounces it produces) settles on the next refresh. Clearing
        # synchronously left the door open to a watcher fire that
        # treated the rebound as a fresh user scroll, which caused the
        # panes to flicker non-stop while one was being scrolled.
        self._syncing_scroll: bool = False

    @property
    def state(self) -> _ReaderState:
        return self._state

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            with Horizontal(id="reader-body"):
                with Vertical(id="reader-chapters-panel"):
                    with Vertical(id="reader-chapter-actions"):
                        yield Button(
                            "Translate chapter",
                            id="reader-translate-chapter",
                            variant="primary",
                        )
                        yield Button(
                            "Approve all",
                            id="reader-approve-chapter",
                            variant="success",
                        )
                    chapters = VerticalScroll(id="reader-chapters")
                    chapters.border_title = "Chapters"
                    yield chapters
                source_pane = VerticalScroll(
                    id="reader-source-pane",
                    classes="reader-pane",
                )
                source_pane.border_title = "Source"
                yield source_pane
                target_pane = VerticalScroll(
                    id="reader-target-pane",
                    classes="reader-pane",
                )
                target_pane.border_title = "Target"
                yield target_pane
            with Vertical(id="reader-status-bar"):
                yield Static("Ready.", id="reader-status", markup=True)
                yield BatchProgressMeter(id="reader-batch-meter")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_state()
        self._provider = self._provider_factory()
        self._render_chapter_list()
        self._render_segment_panes()
        self._refresh_status()
        self._sync_batch_meter()
        self._refresh_timer = self.set_interval(
            _LIVE_REFRESH_SECONDS, self._tick_live_refresh
        )
        # Mirror scroll position between the Source and Target panes so a
        # curator skimming one side keeps the other in lockstep
        # (PRD §4.6 reader UX). The watchers run on this Screen, not on
        # the panes themselves, so we don't have to subclass
        # ``VerticalScroll`` just to register a callback.
        source_pane = self.query_one("#reader-source-pane", VerticalScroll)
        target_pane = self.query_one("#reader-target-pane", VerticalScroll)
        self.watch(source_pane, "scroll_y", self._on_source_scroll_y, init=False)
        self.watch(target_pane, "scroll_y", self._on_target_scroll_y, init=False)

    def on_unmount(self) -> None:
        self._provider = None
        # If a chapter batch was still active when the screen pops, the
        # worker thread keeps draining its current LLM call but no further
        # ticks reach the (now-unmounted) screen. Clear the shared slot
        # so other screens don't see a stale "active" flag.
        if self._active_batch_chapter_id is not None:
            self._publish_batch_progress(
                active=False, summary=None, total=0, paused=False
            )
        self._chapter_queue.clear()
        self._active_batch_chapter_id = None
        if self._refresh_timer is not None:
            self._refresh_timer.stop()
            self._refresh_timer = None

    # ----------------- state helpers -----------------

    def _refresh_state(self) -> None:
        chapters = repo.list_chapters(self._project.engine, self._project.project_id)
        seg_map: dict[str, list[repo.SegmentRow]] = {}
        translatable: list[repo.ChapterRow] = []
        for chap in chapters:
            # Filter image-only / structurally-empty segments out of the
            # navigation queue: they have no human-readable text the
            # curator could meaningfully approve, but the original DOM
            # still passes through reassembly verbatim.
            segs = [
                s
                for s in repo.list_segments(self._project.engine, chap.id)
                if has_translatable_text(s.source_text)
            ]
            if segs:
                seg_map[chap.id] = segs
                translatable.append(chap)
        self._state = _ReaderState(
            chapters=translatable,
            segments_by_chapter=seg_map,
        )
        if self._initial_chapter_id is not None:
            for idx, chap in enumerate(translatable):
                if chap.id == self._initial_chapter_id:
                    self.chapter_idx = idx
                    break
            self._initial_chapter_id = None
        if self.chapter_idx >= len(translatable):
            self.chapter_idx = max(0, len(translatable) - 1)
        current_segs = self._current_segments()
        if self.segment_idx >= len(current_segs):
            self.segment_idx = max(0, len(current_segs) - 1)

    def _current_segments(self) -> list[repo.SegmentRow]:
        if not self._state.chapters:
            return []
        chap = self._state.chapters[self.chapter_idx]
        return self._state.segments_by_chapter.get(chap.id, [])

    def _current_segment(self) -> repo.SegmentRow | None:
        segs = self._current_segments()
        if not segs:
            return None
        return segs[self.segment_idx]

    # ----------------- rendering -----------------

    def _render_chapter_list(self) -> None:
        sidebar = self.query_one("#reader-chapters", VerticalScroll)
        sidebar.remove_children()
        self._chapter_cards.clear()
        if not self._state.chapters:
            sidebar.mount(
                Static(
                    "(no translatable chapters)",
                    classes="-empty",
                    markup=False,
                )
            )
            return
        cards: list[ChapterCard] = []
        for idx in range(len(self._state.chapters)):
            card = ChapterCard(chapter_idx=idx, content=self._chapter_card_text(idx))
            if idx == self.chapter_idx:
                card.add_class("-current")
            cards.append(card)
        self._chapter_cards = cards
        sidebar.mount_all(cards)
        self._scroll_chapter_into_view()

    def _chapter_card_text(self, idx: int) -> str:
        chap = self._state.chapters[idx]
        segs = self._state.segments_by_chapter.get(chap.id, [])
        approved = sum(1 for s in segs if s.status == SegmentStatus.APPROVED)
        translated = sum(
            1
            for s in segs
            if s.status in (SegmentStatus.TRANSLATED, SegmentStatus.APPROVED)
        )
        label = self._chapter_card_label(idx, chap)
        return (
            f"[dim]{idx + 1:>2}.[/dim] {escape(label)}\n"
            f"     [dim]{translated}/{len(segs)} translated · "
            f"{approved}/{len(segs)} approved[/dim]"
        )

    def _chapter_card_label(self, idx: int, chap: repo.ChapterRow) -> str:
        """Pick the sidebar label for one chapter card.

        The leading list-position ordinal (``1.``, ``2.``, …) is
        rendered separately by :meth:`_chapter_card_text`, so this
        helper only contributes the *name* of the chapter:

        * If the chapter has a stored title, use it verbatim.
        * If every chapter in the project lacks a title (e.g. a bare
          dump of XHTML with no headings or TOC), fall back to
          ``Chapter {idx + 1}`` so the sidebar still reads as a
          coherent sequence in line with the leading ordinal.
        * Otherwise mark the unnamed chapter explicitly with
          ``(untitled)`` so the curator can spot the gap without it
          colliding with the rendered list ordinal.
        """

        if chap.title:
            return chap.title
        if self._state.all_untitled:
            return f"Chapter {idx + 1}"
        return "(untitled)"

    def _refresh_chapter_cards(self) -> None:
        for idx, card in enumerate(self._chapter_cards):
            card.update_content(self._chapter_card_text(idx))
            card.set_current(idx == self.chapter_idx)

    def _scroll_chapter_into_view(self) -> None:
        if not self._chapter_cards:
            return
        sidebar = self.query_one("#reader-chapters", VerticalScroll)
        if 0 <= self.chapter_idx < len(self._chapter_cards):
            sidebar.scroll_to_widget(
                self._chapter_cards[self.chapter_idx], animate=False
            )

    def _render_segment_panes(self) -> None:
        source_pane = self.query_one("#reader-source-pane", VerticalScroll)
        target_pane = self.query_one("#reader-target-pane", VerticalScroll)
        source_pane.remove_children()
        target_pane.remove_children()
        self._source_cards.clear()
        self._target_cards.clear()
        segs = self._current_segments()
        if not self._state.chapters:
            source_pane.mount(
                Static(
                    "(no translatable chapters in this project)",
                    classes="reader-pane-empty",
                    markup=False,
                )
            )
            return
        if not segs:
            chap = self._state.chapters[self.chapter_idx]
            source_pane.mount(
                Static(
                    f"({self._chapter_label(chap)}: no segments)",
                    classes="reader-pane-empty",
                    markup=False,
                )
            )
            return
        source_cards: list[SegmentCard] = []
        target_cards: list[SegmentCard] = []
        for seg in segs:
            source_card = SegmentCard(
                segment_id=seg.id,
                status=seg.status,
                content=self._segment_content(seg, side="source"),
            )
            target_card = SegmentCard(
                segment_id=seg.id,
                status=seg.status,
                content=self._segment_content(seg, side="target"),
            )
            self._source_cards[seg.id] = source_card
            self._target_cards[seg.id] = target_card
            source_cards.append(source_card)
            target_cards.append(target_card)
        source_pane.mount_all(source_cards)
        target_pane.mount_all(target_cards)
        # Re-apply translating cue for any in-flight segments still on
        # this chapter (they never persist between chapter changes, but
        # the bookkeeping keeps the cards honest if the user navigates
        # away mid-translate and comes back).
        for sid in self._translating_segment_ids:
            self._mark_translating(sid, True)
        self._highlight_current_segment(scroll=True)

    def _segment_content(self, seg: repo.SegmentRow, *, side: str) -> str:
        glyph = _STATUS_GLYPH.get(seg.status, "·")
        prefix = f"[dim]{glyph}[/dim] "
        if seg.id in self._translating_segment_ids:
            prefix = "[bold $warning]▸ Translating…[/bold $warning] "
        if side == "source":
            body = render_preview(seg.source_text, seg.inline_skeleton)
            return prefix + (body or "[dim](empty)[/dim]")
        if seg.target_text:
            body = render_preview(seg.target_text, seg.inline_skeleton)
            return prefix + (body or "[dim](empty)[/dim]")
        return prefix + "[dim](not yet translated)[/dim]"

    def _highlight_current_segment(self, *, scroll: bool) -> None:
        seg = self._current_segment()
        for sid, card in self._source_cards.items():
            card.set_current(seg is not None and sid == seg.id)
        for sid, card in self._target_cards.items():
            card.set_current(seg is not None and sid == seg.id)
        if scroll and seg is not None:
            source_card = self._source_cards.get(seg.id)
            target_card = self._target_cards.get(seg.id)
            source_pane = self.query_one("#reader-source-pane", VerticalScroll)
            target_pane = self.query_one("#reader-target-pane", VerticalScroll)
            self._begin_syncing_scroll()
            try:
                if source_card is not None:
                    source_pane.scroll_to_widget(source_card, animate=False)
                if target_card is not None:
                    target_pane.scroll_to_widget(target_card, animate=False)
            except Exception:
                self._syncing_scroll = False
                raise
            self.call_after_refresh(self._end_syncing_scroll)

    def _on_source_scroll_y(self, _value: float) -> None:
        self._mirror_scroll(source_to_target=True)

    def _on_target_scroll_y(self, _value: float) -> None:
        self._mirror_scroll(source_to_target=False)

    def _mirror_scroll(self, *, source_to_target: bool) -> None:
        """Anchor the *other* pane to the same segment + offset.

        We sync at the segment level: pick the topmost segment whose
        virtual region contains the originating pane's ``scroll_y``,
        compute the fractional offset within that segment, and scroll
        the mirror pane to the same fractional offset on its matching
        card. Segment-anchored sync handles the common case where the
        source and target cards have different rendered heights — a
        purely pixel-based mirror would drift the longer pane out of
        alignment.
        """

        if self._syncing_scroll:
            return
        if not self._source_cards or not self._target_cards:
            return
        if source_to_target:
            from_pane = self.query_one("#reader-source-pane", VerticalScroll)
            to_pane = self.query_one("#reader-target-pane", VerticalScroll)
            from_cards = self._source_cards
            to_cards = self._target_cards
        else:
            from_pane = self.query_one("#reader-target-pane", VerticalScroll)
            to_pane = self.query_one("#reader-source-pane", VerticalScroll)
            from_cards = self._target_cards
            to_cards = self._source_cards
        anchor = self._anchor_for_scroll(from_pane, from_cards)
        if anchor is None:
            return
        seg_id, frac = anchor
        to_card = to_cards.get(seg_id)
        if to_card is None:
            return
        try:
            to_region = to_card.virtual_region
        except Exception:
            return
        height = max(to_region.height, 1)
        target_y = float(to_region.y) + frac * float(height)
        # Convergence guard: skip the mirror when the destination is
        # already (effectively) aligned. Without this, sub-cell drift
        # in the fractional offset bounces back through the watcher
        # the moment ``scroll_to`` clamps the value.
        if abs(float(to_pane.scroll_y) - target_y) < 1.0:
            return
        self._begin_syncing_scroll()
        try:
            to_pane.scroll_to(y=target_y, animate=False, force=True)
        except Exception:
            self._syncing_scroll = False
            raise
        self.call_after_refresh(self._end_syncing_scroll)

    def _begin_syncing_scroll(self) -> None:
        self._syncing_scroll = True

    def _end_syncing_scroll(self) -> None:
        self._syncing_scroll = False

    @staticmethod
    def _anchor_for_scroll(
        pane: VerticalScroll,
        cards: dict[str, SegmentCard],
    ) -> tuple[str, float] | None:
        """Locate the segment containing the pane's scroll position.

        Returns ``(segment_id, frac)`` where ``frac`` is the 0..1
        offset *within* that segment's card, or ``None`` if no card
        is positioned for the current scroll value (cards still
        unlaid-out, etc.).
        """

        scroll_y = float(pane.scroll_y)
        last_above: tuple[str, float] | None = None
        for sid, card in cards.items():
            try:
                region = card.virtual_region
            except Exception:
                continue
            top = float(region.y)
            height = float(region.height) if region.height else 1.0
            bottom = top + height
            if top <= scroll_y < bottom:
                frac = (scroll_y - top) / height
                if frac < 0.0:
                    frac = 0.0
                elif frac > 1.0:
                    frac = 1.0
                return sid, frac
            if scroll_y >= bottom:
                last_above = sid, 1.0
        return last_above

    def _refresh_segment_card(self, segment_id: str) -> None:
        seg = self._find_segment_by_id(segment_id)
        if seg is None:
            return
        source_card = self._source_cards.get(segment_id)
        target_card = self._target_cards.get(segment_id)
        if source_card is not None:
            source_card.update_status(seg.status)
            source_card.update_content(self._segment_content(seg, side="source"))
        if target_card is not None:
            target_card.update_status(seg.status)
            target_card.update_content(self._segment_content(seg, side="target"))

    def _mark_translating(self, segment_id: str, translating: bool) -> None:
        if translating:
            self._translating_segment_ids.add(segment_id)
        else:
            self._translating_segment_ids.discard(segment_id)
        for cards in (self._source_cards, self._target_cards):
            card = cards.get(segment_id)
            if card is not None:
                card.set_translating(translating)
                # Refresh the body so the "▸ Translating…" prefix appears.
                seg = self._find_segment_by_id(segment_id)
                if seg is not None:
                    side = "source" if cards is self._source_cards else "target"
                    card.update_content(self._segment_content(seg, side=side))

    def _find_segment_by_id(self, segment_id: str) -> repo.SegmentRow | None:
        for segs in self._state.segments_by_chapter.values():
            for seg in segs:
                if seg.id == segment_id:
                    return seg
        return None

    def _refresh_status(self, *, override: str | None = None) -> None:
        widget = self.query_one("#reader-status", Static)
        if override is not None:
            widget.update(override)
            return
        if not self._state.chapters:
            widget.update("No segments to translate.")
            return
        chap = self._state.chapters[self.chapter_idx]
        segs = self._current_segments()
        title = self._chapter_label(chap)
        if not segs:
            widget.update(
                f"Chapter {self.chapter_idx + 1}/{len(self._state.chapters)} "
                f"([b]{escape(title)}[/b]) — no translatable segments"
            )
            return
        seg = segs[self.segment_idx]
        approved = sum(1 for s in segs if s.status == SegmentStatus.APPROVED)
        translated = sum(
            1
            for s in segs
            if s.status in (SegmentStatus.TRANSLATED, SegmentStatus.APPROVED)
        )
        widget.update(
            f"Chapter {self.chapter_idx + 1}/{len(self._state.chapters)} "
            f"([b]{escape(title)}[/b]) — segment "
            f"{self.segment_idx + 1}/{len(segs)} — status: {seg.status} — "
            f"chapter progress: {translated}/{len(segs)} translated, "
            f"{approved}/{len(segs)} approved"
        )

    def _redraw_after_state_change(self, *, segment_id: str | None = None) -> None:
        """Refresh the views after a write that may have moved status.

        When only one segment changed we patch its card; chapter-level
        changes (status counts in the sidebar) get refreshed unconditionally
        because the work is cheap and keeping the sidebar in sync matters
        for the curator's at-a-glance progress check.
        """

        if segment_id is not None:
            self._refresh_segment_card(segment_id)
        self._refresh_chapter_cards()
        self._refresh_status()

    def _chapter_label(self, chap: repo.ChapterRow) -> str:
        """Pick a human-readable label for a chapter row in the status bar.

        Mirrors the sidebar's labelling rules (see
        :meth:`_chapter_card_label`): titled chapters use their stored
        title; otherwise ``(untitled)`` is shown when at least some
        siblings have titles, or ``Chapter N`` (where N is the chapter's
        position in the translatable list) when the entire project is
        untitled. The status bar already renders the chapter ordinal
        separately, so the label intentionally avoids prefixing its
        own.
        """

        if chap.title:
            return chap.title
        if self._state.all_untitled:
            idx = self.chapter_idx
            for i, c in enumerate(self._state.chapters):
                if c.id == chap.id:
                    idx = i
                    break
            return f"Chapter {idx + 1}"
        return "(untitled)"

    def _set_status(self, message: str) -> None:
        self.query_one("#reader-status", Static).update(message)

    # ----------------- live refresh / batch meter -----------------

    def _tick_live_refresh(self) -> None:
        """Periodic refresh so a running batch shows up live.

        We only refresh when something is actually moving: the user is
        translating interactively, OR the app's batch slot says a batch
        is active (and belongs to this project). Skipping the work
        otherwise keeps the Reader cheap when idle.
        """

        progress = self._app_batch_progress()
        active_for_project = (
            progress is not None
            and progress.active
            and progress.project_id == self._project.project_id
        )
        if not active_for_project and not self._translating_segment_ids:
            self._sync_batch_meter()
            return
        prev_status: dict[str, str] = {}
        for segs in self._state.segments_by_chapter.values():
            for seg in segs:
                prev_status[seg.id] = seg.status
        self._refresh_state()
        for segs in self._state.segments_by_chapter.values():
            for seg in segs:
                old = prev_status.get(seg.id)
                if old != seg.status:
                    self._refresh_segment_card(seg.id)
        self._refresh_chapter_cards()
        self._refresh_status()
        self._sync_batch_meter()

    def _app_batch_progress(self) -> BatchProgress | None:
        """Lazy import-free accessor for the app's optional batch slot.

        The Reader survives without an :class:`EpublateApp` parent (e.g.
        in tests that mount the screen on a bare :class:`textual.App`),
        so the slot is treated as advisory.
        """

        from epublate.app.main import BatchProgress, EpublateApp

        app = self.app
        if not isinstance(app, EpublateApp):
            return None
        progress = app.batch_progress
        if not isinstance(progress, BatchProgress):
            return None
        return progress

    def _sync_batch_meter(self) -> None:
        meter = self.query_one("#reader-batch-meter", BatchProgressMeter)
        progress = self._app_batch_progress()
        if (
            progress is None
            or not progress.active
            or progress.project_id != self._project.project_id
        ):
            if self._batch_meter_visible:
                meter.update_snapshot(None)
                self._batch_meter_visible = False
            return
        summary = progress.summary
        total = progress.total
        starting_spend = progress.starting_spend_usd
        if summary is None:
            snapshot = BatchSnapshot(
                attempted=0,
                total=total,
                chapter_count=progress.chapter_count,
                paused=progress.paused,
                cancelling=progress.cancelling,
                project_total_cost_usd=starting_spend or None,
            )
        else:
            snapshot = BatchSnapshot(
                attempted=summary.attempted,
                total=total,
                chapter_count=progress.chapter_count,
                translated=summary.translated,
                cached=summary.cached,
                flagged=summary.flagged,
                failed=summary.failed,
                cost_usd=summary.cost_usd,
                project_total_cost_usd=starting_spend + summary.cost_usd,
                elapsed_s=summary.elapsed_s,
                paused=progress.paused,
                paused_reason=summary.paused_reason,
                cancelling=progress.cancelling,
            )
        meter.update_snapshot(snapshot)
        self._batch_meter_visible = True

    # ----------------- mouse navigation -----------------

    @on(SegmentClicked)
    def _on_segment_clicked(self, message: SegmentClicked) -> None:
        message.stop()
        segs = self._current_segments()
        for idx, seg in enumerate(segs):
            if seg.id == message.segment_id:
                self.segment_idx = idx
                self._highlight_current_segment(scroll=True)
                self._refresh_status()
                return

    @on(ChapterClicked)
    def _on_chapter_clicked(self, message: ChapterClicked) -> None:
        message.stop()
        if message.chapter_idx == self.chapter_idx:
            return
        if 0 <= message.chapter_idx < len(self._state.chapters):
            self.chapter_idx = message.chapter_idx
            self.segment_idx = 0
            self._refresh_chapter_cards()
            self._render_segment_panes()
            self._refresh_status()
            self._scroll_chapter_into_view()

    # ----------------- keyboard navigation -----------------

    def action_next_segment(self) -> None:
        segs = self._current_segments()
        if not segs:
            return
        self.segment_idx = (self.segment_idx + 1) % len(segs)
        self._highlight_current_segment(scroll=True)
        self._refresh_status()

    def action_prev_segment(self) -> None:
        segs = self._current_segments()
        if not segs:
            return
        self.segment_idx = (self.segment_idx - 1) % len(segs)
        self._highlight_current_segment(scroll=True)
        self._refresh_status()

    def action_next_chapter(self) -> None:
        if not self._state.chapters:
            return
        self.chapter_idx = (self.chapter_idx + 1) % len(self._state.chapters)
        self.segment_idx = 0
        self._refresh_chapter_cards()
        self._render_segment_panes()
        self._refresh_status()
        self._scroll_chapter_into_view()

    def action_prev_chapter(self) -> None:
        if not self._state.chapters:
            return
        self.chapter_idx = (self.chapter_idx - 1) % len(self._state.chapters)
        self.segment_idx = 0
        self._refresh_chapter_cards()
        self._render_segment_panes()
        self._refresh_status()
        self._scroll_chapter_into_view()

    def action_first_segment(self) -> None:
        if not self._current_segments():
            return
        self.segment_idx = 0
        self._highlight_current_segment(scroll=True)
        self._refresh_status()

    def action_last_segment(self) -> None:
        segs = self._current_segments()
        if not segs:
            return
        self.segment_idx = len(segs) - 1
        self._highlight_current_segment(scroll=True)
        self._refresh_status()

    # ----------------- translate / retry / accept / edit -----------------

    def action_translate_next(self) -> None:
        seg = self._current_segment()
        if seg is None:
            return
        if self._provider is None:
            self._set_status("LLM provider not initialized.")
            return
        self._mark_translating(seg.id, True)
        self._set_status(f"Translating segment {self.segment_idx + 1}…")
        self._run_translation(segment=seg, bypass_cache=False)

    def action_retry(self) -> None:
        seg = self._current_segment()
        if seg is None:
            return
        if self._provider is None:
            self._set_status("LLM provider not initialized.")
            return
        self._mark_translating(seg.id, True)
        self._set_status(f"Retrying segment {self.segment_idx + 1}…")
        self._run_translation(segment=seg, bypass_cache=True)

    def action_translate_chapter(self) -> None:
        """Batch-translate every pending segment in the current chapter.

        Multiple ``b`` presses queue up: a second chapter requested while
        the first is still draining is appended to ``_chapter_queue`` and
        starts as soon as the active worker finishes. The queue is the
        Reader's local concept; foreign batches (Dashboard) still block
        the start since they share the same ``app.batch_progress`` slot.
        """

        if not self._state.chapters:
            self._set_status("No chapters available.")
            return
        chap = self._state.chapters[self.chapter_idx]
        self._enqueue_chapter_translation(chap.id)

    def _enqueue_chapter_translation(self, chapter_id: str) -> None:
        if self._active_batch_chapter_id is None and self._other_batch_active():
            self._set_status(
                "Another batch is running; wait for it to finish before "
                "translating a chapter."
            )
            return
        if self._active_batch_chapter_id == chapter_id:
            self._set_status("This chapter is already translating.")
            return
        if chapter_id in self._chapter_queue:
            self._set_status("This chapter is already queued.")
            return
        if self._active_batch_chapter_id is None:
            self._start_chapter_batch(chapter_id)
            return
        self._chapter_queue.append(chapter_id)
        active_label = self._chapter_label_by_id(self._active_batch_chapter_id)
        queued_label = self._chapter_label_by_id(chapter_id)
        depth = len(self._chapter_queue)
        self._set_status(
            f"Queued [b]{escape(queued_label)}[/b] "
            f"(translating [b]{escape(active_label)}[/b], {depth} chapter"
            f"{'s' if depth != 1 else ''} queued)."
        )

    def _start_chapter_batch(self, chapter_id: str) -> None:
        if self._provider_factory is None:
            self._set_status("LLM provider not initialized.")
            return
        pending = repo.list_segments_by_status(
            self._project.engine,
            project_id=self._project.project_id,
            status=SegmentStatus.PENDING,
            chapter_ids=(chapter_id,),
        )
        total = len(pending)
        self._active_batch_chapter_id = chapter_id
        self._active_batch_total = total
        # Publish the slot up-front (with summary=None) so the live meter
        # snaps to "0/total" immediately instead of waiting for the first
        # tick to land — matches the Dashboard's UX (PRD §4.6).
        self._publish_batch_progress(
            active=True, summary=None, total=total, paused=False
        )
        self._sync_batch_meter()
        chapter_label = self._chapter_label_by_id(chapter_id)
        if total == 0:
            self._set_status(
                f"[b]{escape(chapter_label)}[/b]: nothing pending; finishing run."
            )
        else:
            self._set_status(
                f"Translating [b]{escape(chapter_label)}[/b]: 0/{total} segments…"
            )
        self._chapter_batch_worker(chapter_id=chapter_id)

    def _other_batch_active(self) -> bool:
        """True when *some other* component reports an active batch.

        Only meaningful when the Reader itself doesn't think it's running
        a batch (callers gate on ``_active_batch_chapter_id is None``).
        """

        progress = self._app_batch_progress()
        return progress is not None and progress.active

    def _publish_batch_progress(
        self,
        *,
        active: bool,
        summary: BatchSummary | None,
        total: int,
        paused: bool,
    ) -> None:
        """Mirror the Reader's batch state on the app slot.

        Keeps the Dashboard / other consumers in sync (PRD §4.6 'live
        updates'). Mutating an attribute is fine because Textual's main
        loop is single-threaded.
        """

        from epublate.app.main import BatchProgress, EpublateApp

        app = self.app
        if not isinstance(app, EpublateApp):
            return
        app.batch_progress = BatchProgress(
            active=active,
            project_id=self._project.project_id if active else None,
            summary=summary,
            total=total,
            paused=paused,
        )

    def _chapter_label_by_id(self, chapter_id: str) -> str:
        for chap in self._state.chapters:
            if chap.id == chapter_id:
                return self._chapter_label(chap)
        return "(unknown chapter)"

    def action_accept(self) -> None:
        seg = self._current_segment()
        if seg is None:
            return
        if not seg.target_text:
            self._set_status("Nothing to accept — translate the segment first.")
            return
        repo.update_segment_translation(
            self._project.engine,
            segment_id=seg.id,
            target_text=seg.target_text,
            status=SegmentStatus.APPROVED,
        )
        repo.append_event(
            self._project.engine,
            project_id=self._project.project_id,
            kind="segment.approved",
            payload={"segment_id": seg.id},
        )
        self._refresh_state()
        self._redraw_after_state_change(segment_id=seg.id)
        self._set_status(f"Approved segment {self.segment_idx + 1}.")

    def action_approve_chapter(self) -> None:
        """Approve every TRANSLATED segment in the current chapter.

        Deliberately conservative scope: ``flagged`` segments stay flagged
        (the validator caught a real issue — placeholder mismatch or a
        locked-glossary violation; the curator still has to look at
        them, see PRD invariant §2), and ``pending`` ones obviously
        can't be approved without a target. The status line reports
        both buckets so the curator knows what was skipped.

        We refresh from the DB before reading status so a segment that
        flipped to ``flagged`` between live-refresh ticks isn't
        accidentally auto-approved.
        """

        if not self._state.chapters:
            self._set_status("No chapters available.")
            return
        self._refresh_state()
        if not self._state.chapters:
            self._set_status("No chapters available.")
            return
        chap = self._state.chapters[self.chapter_idx]
        segs = self._state.segments_by_chapter.get(chap.id, [])
        to_approve = [
            s for s in segs if s.status == SegmentStatus.TRANSLATED and s.target_text
        ]
        skipped_flagged = sum(1 for s in segs if s.status == SegmentStatus.FLAGGED)
        skipped_pending = sum(1 for s in segs if s.status == SegmentStatus.PENDING)
        chap_label = self._chapter_label_by_id(chap.id)
        if not to_approve:
            parts = [f"Nothing to approve in [b]{escape(chap_label)}[/b]"]
            if skipped_flagged:
                parts.append(
                    f"{skipped_flagged} flagged segment"
                    f"{'s' if skipped_flagged != 1 else ''} need review"
                )
            if skipped_pending:
                parts.append(
                    f"{skipped_pending} segment"
                    f"{'s' if skipped_pending != 1 else ''} not yet translated"
                )
            self._set_status(" — ".join(parts) + ".")
            return
        for seg in to_approve:
            assert seg.target_text is not None
            repo.update_segment_translation(
                self._project.engine,
                segment_id=seg.id,
                target_text=seg.target_text,
                status=SegmentStatus.APPROVED,
            )
            repo.append_event(
                self._project.engine,
                project_id=self._project.project_id,
                kind="segment.approved",
                payload={"segment_id": seg.id, "via": "approve_chapter"},
            )
        self._refresh_state()
        for seg in to_approve:
            self._refresh_segment_card(seg.id)
        self._refresh_chapter_cards()
        self._refresh_status()
        notes: list[str] = []
        if skipped_flagged:
            notes.append(f"skipped {skipped_flagged} flagged")
        if skipped_pending:
            notes.append(f"skipped {skipped_pending} pending")
        suffix = f" ({', '.join(notes)})" if notes else ""
        self._set_status(
            f"Approved {len(to_approve)} segment"
            f"{'s' if len(to_approve) != 1 else ''} in "
            f"[b]{escape(chap_label)}[/b]{suffix}."
        )

    def action_edit(self) -> None:
        seg = self._current_segment()
        if seg is None:
            return
        modal = EditTargetScreen(
            source=seg.source_text,
            target=seg.target_text or "",
            skeleton=list(seg.inline_skeleton),
        )
        self.app.push_screen(modal, self._on_edit_finished)

    def action_open_glossary(self) -> None:
        # Imported lazily to keep ``ReaderScreen`` instantiable in tests
        # that don't exercise the Glossary subtree.
        from epublate.app.screens.glossary import GlossaryScreen

        self.app.push_screen(GlossaryScreen(self._project), self._on_glossary_closed)

    def _on_glossary_closed(self, _result: object) -> None:
        # The cascade flow may have flipped segment statuses; reload.
        self._refresh_state()
        self._render_chapter_list()
        self._render_segment_panes()
        self._refresh_status()

    def _on_edit_finished(self, result: str | None) -> None:
        if result is None:
            self._set_status("Edit cancelled.")
            return
        seg = self._current_segment()
        if seg is None:
            return
        repo.update_segment_translation(
            self._project.engine,
            segment_id=seg.id,
            target_text=result,
            status=SegmentStatus.APPROVED,
        )
        repo.append_event(
            self._project.engine,
            project_id=self._project.project_id,
            kind="segment.edited",
            payload={"segment_id": seg.id},
        )
        self._refresh_state()
        self._redraw_after_state_change(segment_id=seg.id)
        self._set_status(f"Saved edit on segment {self.segment_idx + 1}.")

    # ----------------- worker plumbing -----------------

    def _run_translation(self, *, segment: repo.SegmentRow, bypass_cache: bool) -> None:
        provider = self._provider
        if provider is None:
            return
        self._translate_worker(segment=segment, bypass_cache=bypass_cache)

    @work(exclusive=True, group="translate", thread=True)
    def _translate_worker(
        self,
        *,
        segment: repo.SegmentRow,
        bypass_cache: bool,
    ) -> None:
        provider = self._provider
        if provider is None:
            return
        try:
            outcome = translate_segment(
                engine=self._project.engine,
                project_id=self._project.project_id,
                source_lang=self._project.source_lang,
                target_lang=self._project.target_lang,
                style_guide=self._project.style_guide,
                segment=segment,
                provider=provider,
                options=TranslateOptions(model=self._model, bypass_cache=bypass_cache),
            )
        except Exception as exc:
            # Worker boundary: surface every failure as a typed UI message
            # so the curator sees something instead of an unhandled crash.
            self.post_message(PipelineFailed(str(exc), segment_id=segment.id))
            return
        self.post_message(PipelineFinished(outcome))

    @on(PipelineFinished)
    def _handle_pipeline_finished(self, message: PipelineFinished) -> None:
        outcome = message.outcome
        self._mark_translating(outcome.segment_id, False)
        self._refresh_state()
        self._redraw_after_state_change(segment_id=outcome.segment_id)
        suffix = " (cache hit)" if outcome.cache_hit else ""
        self._set_status(
            f"Translated segment{suffix}: "
            f"{outcome.prompt_tokens}→{outcome.completion_tokens} tokens, "
            f"${outcome.cost_usd:.6f}"
        )

    @on(PipelineFailed)
    def _handle_pipeline_failed(self, message: PipelineFailed) -> None:
        if message.segment_id is not None:
            self._mark_translating(message.segment_id, False)
        self._set_status(f"Translation failed: {message.error}")

    # ----------------- chapter batch worker -----------------

    @work(exclusive=False, group="reader-chapter-batch", thread=True)
    def _chapter_batch_worker(self, *, chapter_id: str) -> None:
        provider = self._provider_factory()
        # Chapter translate from the Reader is a "batch of one chapter".
        # We turn the helper-LLM pre-pass on by default so the curator's
        # iterative per-chapter workflow grows the lore bible the same
        # way a full ``b`` batch does (PRD §4.2 phase 3 / M5). If a
        # cheaper helper isn't configured, ``run_batch`` falls back to
        # the translator model itself (per ``BatchOptions`` docs);
        # we resolve up-front so the project's ``helper_model``
        # override and ``$EPUBLATE_LLM_HELPER_MODEL`` actually win.
        try:
            project_overrides = repo.get_llm_overrides(
                self._project.engine, self._project.project_id
            )
        except Exception:
            project_overrides = {}
        try:
            helper_model: str | None = resolve_helper_model(
                self._model, project_overrides=project_overrides
            )
        except Exception:
            helper_model = self._model
        options = BatchOptions(
            model=self._model,
            concurrency=1,
            chapter_ids=(chapter_id,),
            bypass_cache=False,
            pre_pass=True,
            helper_model=helper_model,
        )
        try:
            summary = run_batch(
                engine=self._project.engine,
                project_id=self._project.project_id,
                source_lang=self._project.source_lang,
                target_lang=self._project.target_lang,
                provider=provider,
                options=options,
                on_progress=self._post_chapter_batch_tick,
            )
        except BatchPaused as paused:
            self.post_message(
                ChapterBatchFinished(paused.summary, paused=True, chapter_id=chapter_id)
            )
            return
        except Exception as exc:
            self.post_message(ChapterBatchFailed(str(exc), chapter_id=chapter_id))
            return
        self.post_message(
            ChapterBatchFinished(summary, paused=False, chapter_id=chapter_id)
        )

    def _post_chapter_batch_tick(self, event: BatchProgressEvent) -> None:
        # Called from the worker thread; ``post_message`` is thread-safe.
        self.post_message(ChapterBatchTick(event))

    @on(ChapterBatchTick)
    def _handle_chapter_batch_tick(self, message: ChapterBatchTick) -> None:
        if self._active_batch_chapter_id is None:
            # Stale tick after we cleared state (screen popped, queue
            # cancelled, etc.). Drop it rather than reviving the meter.
            return
        summary = message.event.summary
        self._publish_batch_progress(
            active=True,
            summary=summary,
            total=self._active_batch_total,
            paused=False,
        )
        self._sync_batch_meter()
        self._refresh_status_with_progress(summary)

    def _refresh_status_with_progress(self, summary: BatchSummary) -> None:
        chap_id = self._active_batch_chapter_id
        if chap_id is None:
            return
        chap_label = self._chapter_label_by_id(chap_id)
        depth = len(self._chapter_queue)
        queue_text = (
            f" — {depth} chapter{'s' if depth != 1 else ''} queued" if depth else ""
        )
        self._set_status(
            f"Translating [b]{escape(chap_label)}[/b]: "
            f"{summary.attempted}/{self._active_batch_total} "
            f"(translated {summary.translated}, cached {summary.cached}, "
            f"failed {summary.failed}){queue_text}"
        )

    @on(ChapterBatchFinished)
    def _handle_chapter_batch_finished(self, message: ChapterBatchFinished) -> None:
        s = message.summary
        chap_label = self._chapter_label_by_id(message.chapter_id)
        kind = "paused" if message.paused else "complete"
        self._active_batch_chapter_id = None
        self._refresh_state()
        self._render_segment_panes()
        self._refresh_chapter_cards()
        if message.paused and s.paused_reason:
            # Surface the pause reason verbatim so the curator sees the
            # actionable hint (rate-limit reset window, budget cap dollar
            # value, etc.) without having to dig through the Inbox.
            self._set_status(
                f"Chapter batch paused for [b]{escape(chap_label)}[/b]: "
                f"{escape(s.paused_reason)}"
            )
        else:
            self._set_status(
                f"Chapter batch {kind} for [b]{escape(chap_label)}[/b]: "
                f"translated={s.translated}, cached={s.cached}, "
                f"flagged={s.flagged}, failed={s.failed}, "
                f"cost=${s.cost_usd:.4f}"
            )
        if message.paused:
            # A pause means the next chapter would hit the same wall
            # (budget cap, rate limit, ...). Clear the queue so we don't
            # silently fail-and-pause-again across the rest of the run.
            self._chapter_queue.clear()
            self._publish_batch_progress(
                active=False,
                summary=s,
                total=self._active_batch_total,
                paused=True,
            )
            self._sync_batch_meter()
            return
        if not self._chapter_queue:
            self._publish_batch_progress(
                active=False,
                summary=s,
                total=self._active_batch_total,
                paused=False,
            )
            self._sync_batch_meter()
            return
        next_chapter = self._chapter_queue.pop(0)
        self._start_chapter_batch(next_chapter)

    @on(ChapterBatchFailed)
    def _handle_chapter_batch_failed(self, message: ChapterBatchFailed) -> None:
        chap_label = self._chapter_label_by_id(message.chapter_id)
        self._active_batch_chapter_id = None
        # Hard failure aborts the rest of the queue: a transport / parse
        # error tends to keep failing on the next chapter, and silently
        # marching on would mask the cause from the curator.
        self._chapter_queue.clear()
        self._publish_batch_progress(active=False, summary=None, total=0, paused=False)
        self._sync_batch_meter()
        self._set_status(
            f"Chapter batch failed for [b]{escape(chap_label)}[/b]: {message.error}"
        )

    # ----------------- button wiring -----------------

    @on(Button.Pressed, "#reader-translate-chapter")
    def _on_translate_chapter_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_translate_chapter()

    @on(Button.Pressed, "#reader-approve-chapter")
    def _on_approve_chapter_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        self.action_approve_chapter()


__all__ = [
    "DEFAULT_MODEL",
    "ChapterBatchFailed",
    "ChapterBatchFinished",
    "ChapterBatchTick",
    "ChapterCard",
    "ChapterClicked",
    "PipelineFailed",
    "PipelineFinished",
    "ProviderFactory",
    "ReaderScreen",
    "SegmentCard",
    "SegmentClicked",
]
