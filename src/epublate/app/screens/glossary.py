"""Glossary screen — lore-bible editor (PRD §4.6, §7.4 / M3).

What lives here:

* A filterable :class:`textual.widgets.DataTable` of glossary entries.
* A detail pane showing aliases + revision history for the highlighted
  entry (PRD F-LB-6).
* Modal dialogs to create / edit / delete entries and to confirm a
  cascade re-translation (PRD §7.5 / F-LB-7).
* Status-toggling shortcuts so the curator can promote a candidate from
  ``proposed`` → ``confirmed`` → ``locked`` with a single keystroke.

All DB writes go through :mod:`epublate.db.repo` and the cascade flow
runs in a Textual worker per the TUI rule (the UI never blocks).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Protocol, cast

from rich.markup import escape as escape_markup
from sqlalchemy.engine import Engine
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    TextArea,
)

from epublate.app.widgets import BatchStatusBar
from epublate.core.project import Project
from epublate.core.segmentation import PLACEHOLDER_RE
from epublate.db import repo
from epublate.db.schema import GlossaryStatus
from epublate.glossary import (
    CascadeCandidate,
    GlossaryEntryWithAliases,
    analyze_pair,
    cascade_retranslate,
    compute_affected,
    find_near_duplicates,
)
from epublate.glossary.models import (
    EntityType,
    GenderTag,
    GlossaryStatusLiteral,
)


class GlossarySource(Protocol):
    """Minimal contract a screen owner must satisfy.

    Both :class:`epublate.core.project.Project` and
    :class:`epublate.lore.lore.LoreBook` implement this implicitly —
    same field names, same semantics. The Glossary screen calls this
    contract instead of holding a concrete :class:`Project` so it can
    edit Lore Books without forking the whole UI.
    """

    @property
    def engine(self) -> Engine: ...
    @property
    def project_id(self) -> str: ...
    @property
    def name(self) -> str: ...
    @property
    def source_lang(self) -> str: ...
    @property
    def target_lang(self) -> str: ...


_ENTITY_TYPES: tuple[EntityType, ...] = (
    "character",
    "place",
    "organization",
    "event",
    "item",
    "date_or_time",
    "phrase",
    "term",
    "other",
)
_GENDERS: tuple[GenderTag, ...] = (
    "feminine",
    "masculine",
    "neuter",
    "common",
    "unspecified",
)
_STATUSES: tuple[GlossaryStatusLiteral, ...] = ("proposed", "confirmed", "locked")


# ---------------------------------------------------------------------------
# Edit modal
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _EntryDraft:
    """Form-shape mirror of a glossary entry the modal lets a curator edit.

    ``source_term`` defaults to ``""`` and stays optional only inside a
    Lore Book context (PRD F-LB-3 / F-LB-9). Project-scoped editing
    enforces a non-empty source term in :meth:`EntryEditScreen.action_save`.
    """

    source_term: str = ""
    target_term: str = ""
    type: EntityType = "term"
    status: GlossaryStatusLiteral = "proposed"
    gender: GenderTag | None = None
    notes: str = ""
    source_aliases: str = ""
    target_aliases: str = ""

    @classmethod
    def from_entry(cls, entry: GlossaryEntryWithAliases) -> _EntryDraft:
        return cls(
            source_term=entry.source_term or "",
            target_term=entry.target_term,
            type=entry.entry.type,
            status=entry.status,
            gender=entry.entry.gender,
            notes=entry.entry.notes or "",
            source_aliases=", ".join(entry.source_aliases),
            target_aliases=", ".join(entry.target_aliases),
        )


@dataclass(slots=True)
class _EntryDraftResult:
    draft: _EntryDraft


def _split_aliases(raw: str) -> list[str]:
    return [piece.strip() for piece in raw.split(",") if piece.strip()]


class EntryEditScreen(ModalScreen[_EntryDraftResult | None]):
    """Modal for create/edit of a glossary entry."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    EntryEditScreen {
        align: center middle;
    }
    EntryEditScreen #entry-box {
        width: 90%;
        height: 90%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    EntryEditScreen .row {
        height: auto;
        padding: 0 0 1 0;
    }
    EntryEditScreen .row Label {
        width: 18;
    }
    EntryEditScreen Input,
    EntryEditScreen Select {
        width: 1fr;
    }
    EntryEditScreen #entry-notes {
        height: 5;
    }
    EntryEditScreen #entry-help {
        height: 1;
        color: $text-muted;
    }
    EntryEditScreen #entry-error {
        height: auto;
        min-height: 1;
        color: $error;
    }
    """

    def __init__(
        self,
        draft: _EntryDraft,
        *,
        title: str,
        source_term_required: bool = True,
        source_lang: str | None = None,
        target_lang: str | None = None,
    ) -> None:
        super().__init__()
        self._draft = draft
        self._title = title
        self._source_term_required = source_term_required
        # Stored so :meth:`action_save` can run the particle-symmetry
        # check (PRD F-LB-3): a glossary entry whose source has a
        # leading article/preposition while the target doesn't (or
        # vice versa) explodes into ``"na na Europa"`` style doubling
        # at translation time. ``None`` skips the check, which is what
        # legacy callers (and Lore Book editors with no project lang
        # context) get implicitly.
        self._source_lang = source_lang
        self._target_lang = target_lang

    def compose(self) -> ComposeResult:
        with Vertical(id="entry-box"):
            yield Label(self._title)
            with Horizontal(classes="row"):
                yield Label("Source term:")
                yield Input(value=self._draft.source_term, id="entry-source")
            with Horizontal(classes="row"):
                yield Label("Target term:")
                yield Input(value=self._draft.target_term, id="entry-target")
            with Horizontal(classes="row"):
                yield Label("Type:")
                yield Select(
                    [(t, t) for t in _ENTITY_TYPES],
                    value=self._draft.type,
                    id="entry-type",
                    allow_blank=False,
                )
            with Horizontal(classes="row"):
                yield Label("Status:")
                yield Select(
                    [(s, s) for s in _STATUSES],
                    value=self._draft.status,
                    id="entry-status",
                    allow_blank=False,
                )
            with Horizontal(classes="row"):
                yield Label("Gender:")
                yield Select(
                    [(g, g) for g in _GENDERS],
                    value=self._draft.gender or Select.NULL,
                    id="entry-gender",
                    allow_blank=True,
                )
            with Horizontal(classes="row"):
                yield Label("Source aliases:")
                yield Input(
                    value=self._draft.source_aliases,
                    placeholder="comma-separated",
                    id="entry-src-aliases",
                )
            with Horizontal(classes="row"):
                yield Label("Target aliases:")
                yield Input(
                    value=self._draft.target_aliases,
                    placeholder="comma-separated",
                    id="entry-tgt-aliases",
                )
            yield Label("Notes:")
            yield TextArea(self._draft.notes, id="entry-notes")
            yield Static("", id="entry-error", markup=False)
            yield Static(
                "Enter or Ctrl+S to save, Escape to cancel.",
                id="entry-help",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#entry-source", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        # Enter on any single-line Input (source/target term, alias
        # lists) submits the modal. The notes ``TextArea`` keeps
        # Enter as content because multi-line notes are common —
        # only Ctrl+S works from the notes field.
        del event
        self.action_save()

    def action_save(self) -> None:
        source_term = self.query_one("#entry-source", Input).value.strip()
        target_term = self.query_one("#entry-target", Input).value.strip()
        if not target_term:
            self._set_error("Target term is required.")
            self.app.bell()
            return
        if self._source_term_required and not source_term:
            self._set_error(
                "Source term is required for project-scoped glossary entries."
            )
            self.app.bell()
            return
        # Particle-symmetry check (PRD F-LB-3 / glossary-invariants §1).
        # A glossary entry whose source has a leading article /
        # preposition while the target doesn't (or vice versa) makes
        # the translator double up function words (``"na na Europa"``
        # for ``Europe → na Europa``). Refuse the save with a
        # curator-friendly message so they can choose between lemma
        # form (drop both) or symmetric form (add the missing
        # particle).
        if self._source_lang is not None or self._target_lang is not None:
            symmetry = analyze_pair(
                source_term=source_term or None,
                target_term=target_term,
                source_lang=self._source_lang,
                target_lang=self._target_lang,
            )
            if not symmetry.symmetric:
                self._set_error(symmetry.message)
                self.app.bell()
                return
        type_value = self.query_one("#entry-type", Select).value
        status_value = self.query_one("#entry-status", Select).value
        gender_select = self.query_one("#entry-gender", Select).value
        gender: GenderTag | None
        if gender_select is Select.NULL:
            gender = None
        else:
            gender = cast(GenderTag, gender_select)
        notes = self.query_one("#entry-notes", TextArea).text.strip() or ""
        src_aliases = self.query_one("#entry-src-aliases", Input).value
        tgt_aliases = self.query_one("#entry-tgt-aliases", Input).value
        result = _EntryDraft(
            source_term=source_term,
            target_term=target_term,
            type=cast(EntityType, type_value),
            status=cast(GlossaryStatusLiteral, status_value),
            gender=gender,
            notes=notes,
            source_aliases=src_aliases,
            target_aliases=tgt_aliases,
        )
        self.dismiss(_EntryDraftResult(draft=result))

    def _set_error(self, message: str) -> None:
        import contextlib

        with contextlib.suppress(Exception):
            self.query_one("#entry-error", Static).update(message)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Cascade modal
# ---------------------------------------------------------------------------


class CascadeConfirmScreen(ModalScreen[bool]):
    """Confirmation modal for the cascade re-translation flow (PRD §7.5)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "confirm", "Yes", show=True),
        Binding("n", "cancel", "No", show=True),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    CascadeConfirmScreen {
        align: center middle;
    }
    CascadeConfirmScreen #cascade-box {
        width: 70%;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    CascadeConfirmScreen #cascade-list {
        height: 1fr;
        margin: 1 0;
    }
    """

    def __init__(
        self,
        *,
        entry: GlossaryEntryWithAliases,
        candidates: list[CascadeCandidate],
    ) -> None:
        super().__init__()
        self._entry = entry
        self._candidates = candidates

    def compose(self) -> ComposeResult:
        with Vertical(id="cascade-box"):
            yield Label(
                f"Cascade re-translation: {len(self._candidates)} segments "
                f"affected by changes to {self._entry.source_term!r}."
            )
            preview = (
                "\n".join(
                    f"  • [{c.reason}] {c.source_text[:80]}…"
                    for c in self._candidates[:10]
                )
                or "  (no segments)"
            )
            yield Static(preview, id="cascade-list", markup=False)
            yield Static(
                "Press [b]y[/b] to revert these segments to pending, "
                "[b]n[/b] to keep current translations.",
                markup=True,
            )

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Merge duplicates modal
# ---------------------------------------------------------------------------


class MergeDuplicatesScreen(ModalScreen[int]):
    """Confirm + apply a "merge all duplicate source terms" pass.

    Renders the duplicate groups returned by
    :func:`epublate.db.repo.find_duplicate_source_terms` so the
    curator can review the auto-picked winners before pressing
    ``y`` to merge them all (status-priority winner, generic ``term``
    rows folded in as alias). On ``y`` the dismiss value is the count
    of groups merged; ``n`` / ``Esc`` dismisses with ``0``.

    The merge itself runs in a Textual worker on the calling
    :class:`GlossaryScreen` (TUI rule §1: never block the UI thread)
    — this modal only collects the curator's intent.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "confirm", "Yes", show=True),
        Binding("n", "cancel", "No", show=True),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    MergeDuplicatesScreen {
        align: center middle;
    }
    MergeDuplicatesScreen #merge-box {
        width: 80%;
        max-height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    MergeDuplicatesScreen #merge-list {
        height: 1fr;
        margin: 1 0;
    }
    """

    def __init__(
        self,
        groups: list[list[GlossaryEntryWithAliases]],
    ) -> None:
        super().__init__()
        self._groups = groups

    def compose(self) -> ComposeResult:
        with Vertical(id="merge-box"):
            yield Label(
                f"Found {len(self._groups)} duplicate source-term group(s). "
                "Auto-merging keeps the winner shown first and folds the "
                "other entries' source/target spellings in as aliases."
            )
            preview_lines: list[str] = []
            for group in self._groups[:10]:
                head = group[0]
                losers = group[1:]
                head_label = (
                    f"  • [b]{head.source_term}[/b] → keep "
                    f"[i]{head.target_term}[/i] "
                    f"({head.entry.type}, {head.status})"
                )
                preview_lines.append(head_label)
                for los in losers:
                    preview_lines.append(
                        f"      ↳ fold [i]{los.target_term}[/i] "
                        f"({los.entry.type}, {los.status}) into aliases"
                    )
            if len(self._groups) > 10:
                preview_lines.append(f"  … and {len(self._groups) - 10} more")
            preview = "\n".join(preview_lines) or "  (no groups)"
            yield Static(preview, id="merge-list", markup=True)
            yield Static(
                "Press [b]y[/b] to merge all groups, [b]n[/b] to cancel.",
                markup=True,
            )

    def action_confirm(self) -> None:
        self.dismiss(len(self._groups))

    def action_cancel(self) -> None:
        self.dismiss(0)


# ---------------------------------------------------------------------------
# Show-occurrences modal
# ---------------------------------------------------------------------------


def _format_occurrence_snippet(
    text: str,
    *,
    span_start: int | None,
    span_end: int | None,
    max_chars: int = 80,
) -> str:
    """Centre a snippet on the matched span and wrap it in ``«…»``.

    Output length is guaranteed to be ≤ ``max_chars`` (except the
    pathological case where the match itself is longer than the
    budget, in which case we still emit the wrapped match so the
    curator can see *what* matched). Whitespace inside the snippet
    is collapsed so paragraph-level matches render on one row, and
    ``«…»`` guillemets are pure ASCII-friendly so the snapshot
    tests stay deterministic and Rich markup parsers don't have
    to play.

    Segment text carries opaque ``[[T0]]``/``[[/T0]]`` placeholders
    for inline tags (PRD §6, format-handling rule §1). We strip
    those *after* slicing so the curator sees clean prose and the
    spans (computed against the placeholder-bearing original) stay
    valid. The final string is also markup-escaped so any literal
    ``[..]`` survivors can't trip Rich's parser when ``DataTable``
    renders the cell.

    When the span is missing (the target column has no spans, and
    legacy mentions written before span tracking landed have
    ``None`` for both ends) we just head-truncate the text.
    """

    flat = " ".join(PLACEHOLDER_RE.sub("", text).split())
    if not flat:
        return ""
    if span_start is None or span_end is None or span_start >= span_end:
        truncated = flat if len(flat) <= max_chars else flat[: max_chars - 1] + "…"
        return escape_markup(truncated)

    match = " ".join(PLACEHOLDER_RE.sub("", text[span_start:span_end]).split())
    pre = " ".join(PLACEHOLDER_RE.sub("", text[:span_start]).split())
    post = " ".join(PLACEHOLDER_RE.sub("", text[span_end:]).split())

    decorated = f"«{match}»"
    pre_sep = " " if pre else ""
    post_sep = " " if post else ""
    budget = max_chars - len(decorated) - len(pre_sep) - len(post_sep)
    if budget <= 0:
        clipped = decorated[:max_chars] if len(decorated) > max_chars else decorated
        return escape_markup(clipped)

    pre_budget = budget // 2
    post_budget = budget - pre_budget
    if len(pre) > pre_budget:
        pre = "…" + pre[-(pre_budget - 1) :] if pre_budget > 1 else "…"
    if len(post) > post_budget:
        post = post[: post_budget - 1] + "…" if post_budget > 1 else "…"
    return escape_markup(f"{pre}{pre_sep}{decorated}{post_sep}{post}")


class OccurrencesScreen(ModalScreen[None]):
    """Read-only list of every segment that referenced a glossary entry.

    Rendered from :func:`epublate.db.repo.list_occurrences` (single
    join, no per-row queries) so the modal opens instantly even on
    long books. The ``Where`` column shows the chapter spine index
    plus title, ``Seg`` is the in-chapter segment index, and the
    snippet columns wrap the matched span in ``«…»``. Curators
    typically use this to spot mis-applied entries: e.g. an entry
    locked to a parliamentary "Câmara" sense that's also firing on
    sentences about a residential ``house``.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "close", "Close", show=True),
        Binding("escape", "close", "Close", show=False),
    ]

    DEFAULT_CSS = """
    OccurrencesScreen {
        align: center middle;
    }
    OccurrencesScreen #occurrences-box {
        width: 90%;
        height: 80%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    OccurrencesScreen #occurrences-table {
        height: 1fr;
        margin: 1 0;
    }
    """

    def __init__(
        self,
        *,
        entry: GlossaryEntryWithAliases,
        occurrences: list[repo.OccurrenceRow],
    ) -> None:
        super().__init__()
        self._entry = entry
        self._occurrences = occurrences

    def compose(self) -> ComposeResult:
        with Vertical(id="occurrences-box"):
            label = self._entry.source_term or self._entry.target_term
            segments = len({o.segment_id for o in self._occurrences})
            yield Label(
                f"Occurrences of [b]{label}[/b] → [b]{self._entry.target_term}[/b]: "
                f"{len(self._occurrences)} mention(s) across {segments} segment(s).",
                markup=True,
            )
            table: DataTable[str] = DataTable(
                id="occurrences-table", zebra_stripes=True, cursor_type="row"
            )
            table.add_columns("Where", "Seg", "Source", "Target")
            for occ in self._occurrences:
                where = f"#{occ.chapter_spine_idx + 1}" + (
                    f" {occ.chapter_title}" if occ.chapter_title else ""
                )
                src = _format_occurrence_snippet(
                    occ.source_text,
                    span_start=occ.source_span_start,
                    span_end=occ.source_span_end,
                )
                tgt = _format_occurrence_snippet(
                    occ.target_text or "",
                    span_start=None,
                    span_end=None,
                )
                table.add_row(where, str(occ.segment_idx + 1), src, tgt)
            yield table
            yield Static(
                "Press [b]q[/b] / [b]Esc[/b] to close.",
                markup=True,
            )

    def action_close(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Delete confirm modal
# ---------------------------------------------------------------------------


class DeleteConfirmScreen(ModalScreen[bool]):
    """Tiny confirm-or-cancel modal for destructive entry actions."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "confirm", "Yes", show=True),
        Binding("n", "cancel", "No", show=True),
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    DEFAULT_CSS = """
    DeleteConfirmScreen {
        align: center middle;
    }
    DeleteConfirmScreen #delete-box {
        width: 60%;
        height: auto;
        border: round $error;
        padding: 1 2;
        background: $surface;
    }
    """

    def __init__(self, *, source_term: str) -> None:
        super().__init__()
        self._source_term = source_term

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-box"):
            yield Label(f"Delete entry {self._source_term!r}?")
            yield Static("Press [b]y[/b] to delete, [b]n[/b] to cancel.")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ---------------------------------------------------------------------------
# Glossary screen
# ---------------------------------------------------------------------------


class CascadeFinished(Message):
    """Posted by the cascade worker when it's done."""

    def __init__(self, count: int, source_term: str) -> None:
        super().__init__()
        self.count = count
        self.source_term = source_term


class CascadeFailed(Message):
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


class GlossaryScreen(Screen[None]):
    """Glossary table + detail pane + edit/cascade flows.

    The screen is reused by Lore Books (Phase 2 of the Lore Books rollout):
    the owner is typed as :class:`GlossarySource`, a structural protocol
    that both :class:`epublate.core.project.Project` and
    :class:`epublate.lore.lore.LoreBook` satisfy. When the owner is a
    Lore Book (no chapters/segments) the cascade action is disabled.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("n", "new_entry", "New", show=True),
        Binding("e", "edit_entry", "Edit", show=True),
        Binding("d", "delete_entry", "Delete", show=True),
        Binding("l", "set_status_locked", "Lock", show=True),
        Binding("c", "set_status_confirmed", "Confirm", show=True),
        Binding("p", "set_status_proposed", "Propose", show=True),
        Binding("r", "cascade", "Cascade", show=True),
        Binding("m", "merge_duplicates", "Cleanup dupes", show=True),
        Binding("o", "show_occurrences", "Occurrences", show=True),
        Binding("f", "cycle_filter", "Filter", show=True),
        Binding("q", "app.pop_screen", "Back", show=True),
        # ``escape`` mirrors ``q`` so curators can back out with the
        # universal "cancel" key; hidden from the footer to keep the
        # binding bar concise.
        Binding("escape", "app.pop_screen", "Back", show=False),
    ]

    DEFAULT_CSS = """
    GlossaryScreen #glossary-body {
        height: 1fr;
    }
    GlossaryScreen #glossary-table {
        height: 1fr;
        border: round $primary;
    }
    GlossaryScreen #glossary-detail {
        width: 40%;
        height: 1fr;
        padding: 1 2;
        border: round $primary;
    }
    GlossaryScreen #glossary-status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    """

    filter_status: reactive[str] = reactive("all")

    def __init__(
        self,
        source: GlossarySource | Project,
        *,
        cascade_enabled: bool | None = None,
    ) -> None:
        super().__init__()
        self._project = source
        self._entries: list[GlossaryEntryWithAliases] = []
        self._row_to_entry: dict[str, str] = {}
        # Cached batch counts so we don't N+1 the DB each render. Refreshed
        # alongside ``_entries`` in :meth:`_refresh_entries`. Missing keys
        # are treated as :class:`MentionCounts()` (zero / zero).
        self._mention_counts: dict[str, repo.MentionCounts] = {}
        # Cascade only makes sense in a translation context (segments
        # in the DB). Lore Books carry no chapters, so we infer the
        # default from ``isinstance(source, Project)`` and let the
        # caller override.
        self._cascade_enabled = (
            cascade_enabled
            if cascade_enabled is not None
            else isinstance(source, Project)
        )
        # Same reasoning for source_term: a Lore Book accepts
        # target-only entries, projects don't.
        self._source_term_required = isinstance(source, Project)

    @property
    def entries(self) -> list[GlossaryEntryWithAliases]:
        return self._entries

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            with Horizontal(id="glossary-body"):
                table: DataTable[str] = DataTable(
                    id="glossary-table", zebra_stripes=True, cursor_type="row"
                )
                table.add_columns(
                    "Type", "Status", "Source", "Target", "Aliases", "Uses"
                )
                yield table
                yield Static("(select an entry)", id="glossary-detail", markup=True)
            yield Static("Ready.", id="glossary-status")
        yield BatchStatusBar(id="glossary-batch-status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_entries()

    # ---------------- state helpers ----------------

    def _refresh_entries(self) -> None:
        self._entries = repo.list_glossary_entries(
            self._project.engine, self._project.project_id
        )
        # One aggregate query per refresh keeps the row render at O(N)
        # in Python; without the batch we'd do a SELECT-per-row inside
        # the table loop and stall on books with thousands of entries.
        self._mention_counts = repo.count_mentions_per_entry(
            self._project.engine, self._project.project_id
        )
        self._render_table()
        self._render_detail()

    def _render_table(self) -> None:
        table = self.query_one("#glossary-table", DataTable)
        table.clear()
        self._row_to_entry.clear()
        rows = [
            e
            for e in self._entries
            if self.filter_status == "all" or e.status == self.filter_status
        ]
        for ent in rows:
            aliases = ", ".join(ent.source_aliases) or "—"
            # Render a friendly placeholder for target-only Lore Book
            # entries so the column stays aligned. The "(target-only)"
            # tag makes the row's lore-book provenance obvious without
            # forcing the curator to open the detail pane.
            source_label = ent.source_term or "(target-only)"
            counts = self._mention_counts.get(ent.id, repo.MentionCounts())
            uses_cell = (
                f"{counts.mentions} ({counts.segments}s)" if counts.mentions else "—"
            )
            row_key = table.add_row(
                ent.entry.type,
                ent.status,
                source_label,
                ent.target_term,
                aliases,
                uses_cell,
                key=ent.id,
            )
            self._row_to_entry[str(row_key)] = ent.id
        self._set_status(
            f"{len(rows)}/{len(self._entries)} entries — filter: {self.filter_status}"
        )

    def _render_detail(self) -> None:
        detail = self.query_one("#glossary-detail", Static)
        ent = self._highlighted_entry()
        if ent is None:
            detail.update("(no entries — press [b]n[/b] to create one)")
            return
        revisions = repo.list_glossary_revisions(self._project.engine, ent.id)
        history = (
            "\n".join(
                f"  - {r.prev_target_term!r} → {r.new_target_term!r}"
                + (f" ({r.reason})" if r.reason else "")
                for r in revisions
            )
            or "  (no revisions)"
        )
        counts = self._mention_counts.get(ent.id, repo.MentionCounts())
        mentions_line = (
            f"{counts.mentions} (across {counts.segments} segment"
            f"{'' if counts.segments == 1 else 's'})"
            if counts.mentions
            else "0 — never used yet"
        )
        notes = ent.entry.notes or ""
        source_label = ent.source_term or "[i](target-only)[/i]"
        body = "\n".join(
            [
                f"[b]{source_label}[/b] → [b]{ent.target_term}[/b]",
                f"  type: {ent.entry.type}    status: {ent.status}"
                + (f"    gender: {ent.entry.gender}" if ent.entry.gender else "")
                + ("" if ent.source_known else "    (soft-lock if locked)"),
                "",
                f"[b]Source aliases[/b]: {', '.join(ent.source_aliases) or '—'}",
                f"[b]Target aliases[/b]: {', '.join(ent.target_aliases) or '—'}",
                "",
                f"[b]Mentions[/b]: {mentions_line}    "
                "(press [b]o[/b] to view occurrences)",
                "",
                "[b]Notes[/b]:",
                f"  {notes or '—'}",
                "",
                "[b]Revisions[/b]:",
                history,
            ]
        )
        detail.update(body)

    def _set_status(self, text: str) -> None:
        self.query_one("#glossary-status", Static).update(text)

    def _highlighted_entry(self) -> GlossaryEntryWithAliases | None:
        table = self.query_one("#glossary-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        entry_id = self._row_to_entry.get(str(row_key))
        if entry_id is None:
            return None
        for ent in self._entries:
            if ent.id == entry_id:
                return ent
        return None

    # ---------------- actions ----------------

    def action_cycle_filter(self) -> None:
        order = ["all", *_STATUSES]
        idx = order.index(self.filter_status) if self.filter_status in order else 0
        self.filter_status = order[(idx + 1) % len(order)]
        self._render_table()
        self._render_detail()

    def action_new_entry(self) -> None:
        modal = EntryEditScreen(
            _EntryDraft(),
            title="New glossary entry",
            source_term_required=self._source_term_required,
            source_lang=self._project.source_lang,
            target_lang=self._project.target_lang,
        )
        self.app.push_screen(modal, self._on_entry_created)

    def _on_entry_created(self, result: _EntryDraftResult | None) -> None:
        if result is None:
            self._set_status("Cancelled.")
            return
        draft = result.draft
        source_term = draft.source_term.strip() or None
        source_known = source_term is not None
        repo.create_glossary_entry(
            self._project.engine,
            project_id=self._project.project_id,
            source_term=source_term,
            target_term=draft.target_term,
            type=draft.type,
            status=draft.status,
            gender=draft.gender,
            notes=draft.notes or None,
            source_aliases=_split_aliases(draft.source_aliases),
            target_aliases=_split_aliases(draft.target_aliases),
            source_known=source_known,
        )
        repo.append_event(
            self._project.engine,
            project_id=self._project.project_id,
            kind="glossary.created",
            payload={
                "source_term": source_term,
                "target_term": draft.target_term,
            },
        )
        self._refresh_entries()
        label = source_term or draft.target_term
        self._set_status(f"Created entry {label!r}.")

    def action_edit_entry(self) -> None:
        ent = self._highlighted_entry()
        if ent is None:
            return
        modal = EntryEditScreen(
            _EntryDraft.from_entry(ent),
            title=f"Edit {(ent.source_term or ent.target_term)!r}",
            source_term_required=self._source_term_required,
            source_lang=self._project.source_lang,
            target_lang=self._project.target_lang,
        )
        self.app.push_screen(modal, _make_edit_callback(self, ent.id))

    def apply_edit(self, entry_id: str, result: _EntryDraftResult | None) -> None:
        if result is None:
            self._set_status("Cancelled.")
            return
        draft = result.draft
        repo.update_glossary_entry(
            self._project.engine,
            entry_id=entry_id,
            target_term=draft.target_term,
            status=draft.status,
            type=draft.type,
            gender=draft.gender,
            notes=draft.notes or None,
            reason="curator-edit",
        )
        repo.set_aliases(
            self._project.engine,
            entry_id=entry_id,
            source_aliases=_split_aliases(draft.source_aliases),
            target_aliases=_split_aliases(draft.target_aliases),
        )
        repo.append_event(
            self._project.engine,
            project_id=self._project.project_id,
            kind="glossary.updated",
            payload={
                "entry_id": entry_id,
                "source_term": draft.source_term,
                "target_term": draft.target_term,
                "status": draft.status,
            },
        )
        self._refresh_entries()
        self._set_status(f"Saved {draft.source_term!r}.")

    def action_delete_entry(self) -> None:
        ent = self._highlighted_entry()
        if ent is None:
            return
        label = ent.source_term or ent.target_term
        modal = DeleteConfirmScreen(source_term=label)
        self.app.push_screen(modal, _make_delete_callback(self, ent.id, label))

    def apply_delete(self, entry_id: str, source_term: str, confirmed: bool) -> None:
        if not confirmed:
            self._set_status("Delete cancelled.")
            return
        repo.delete_glossary_entry(self._project.engine, entry_id)
        repo.append_event(
            self._project.engine,
            project_id=self._project.project_id,
            kind="glossary.deleted",
            payload={"entry_id": entry_id, "source_term": source_term},
        )
        self._refresh_entries()
        self._set_status(f"Deleted entry {source_term!r}.")

    def action_set_status_locked(self) -> None:
        self._set_entry_status(GlossaryStatus.LOCKED)

    def action_set_status_confirmed(self) -> None:
        self._set_entry_status(GlossaryStatus.CONFIRMED)

    def action_set_status_proposed(self) -> None:
        self._set_entry_status(GlossaryStatus.PROPOSED)

    def _set_entry_status(self, status: str) -> None:
        ent = self._highlighted_entry()
        if ent is None:
            return
        label = ent.source_term or ent.target_term
        if ent.status == status:
            self._set_status(f"{label!r} already {status}.")
            return
        repo.update_glossary_entry(
            self._project.engine,
            entry_id=ent.id,
            status=cast(GlossaryStatusLiteral, status),
            reason=f"status->{status}",
        )
        repo.append_event(
            self._project.engine,
            project_id=self._project.project_id,
            kind="glossary.status_changed",
            payload={
                "entry_id": ent.id,
                "source_term": ent.source_term,
                "target_term": ent.target_term,
                "from": ent.status,
                "to": status,
            },
        )
        self._refresh_entries()
        self._set_status(f"{label!r} → {status}.")

    def action_show_occurrences(self) -> None:
        ent = self._highlighted_entry()
        if ent is None:
            self._set_status("No entry highlighted.")
            return
        occurrences = repo.list_occurrences(
            self._project.engine,
            project_id=self._project.project_id,
            entry_id=ent.id,
        )
        label = ent.source_term or ent.target_term
        if not occurrences:
            self._set_status(f"No recorded occurrences for {label!r}.")
            return
        modal = OccurrencesScreen(entry=ent, occurrences=occurrences)
        self.app.push_screen(modal)
        self._set_status(
            f"{label!r}: {len(occurrences)} mention(s) "
            f"in {len({o.segment_id for o in occurrences})} segment(s)."
        )

    def action_merge_duplicates(self) -> None:
        # Fuzzy + exact in a single pass: the canonical-form pass
        # catches the historical pairs that motivated this flow
        # ("HIPC" vs "HIPC initiative", "FIFA" vs broken-paren
        # "(FIFA"), and the Levenshtein guard catches typos that
        # canonicalisation alone misses. Per
        # ``glossary-invariants.mdc`` §4 (no silent merges) the
        # MergeDuplicatesScreen still asks the curator to confirm
        # each group before any DB write happens.
        all_entries = repo.list_glossary_entries(
            self._project.engine, self._project.project_id
        )
        groups = find_near_duplicates(all_entries)
        if not groups:
            self._set_status("No duplicate or near-duplicate entries found.")
            return
        modal = MergeDuplicatesScreen(groups)
        self.app.push_screen(modal, _make_merge_callback(self, groups))

    def apply_merge(
        self,
        groups: list[list[GlossaryEntryWithAliases]],
        confirmed_count: int | None,
    ) -> None:
        if not confirmed_count:
            self._set_status("Merge cancelled.")
            return
        merged = 0
        for group in groups:
            if len(group) < 2:
                continue
            winner = group[0]
            losers = [e.id for e in group[1:]]
            merged += repo.merge_glossary_entries(
                self._project.engine,
                winner_id=winner.id,
                loser_ids=losers,
                reason="curator:merge_duplicates",
            )
        repo.append_event(
            self._project.engine,
            project_id=self._project.project_id,
            kind="glossary.duplicates_merged",
            payload={"groups": len(groups), "rows_removed": merged},
        )
        self._refresh_entries()
        self._set_status(
            f"Merged {len(groups)} duplicate group(s); removed {merged} row(s)."
        )

    def action_cascade(self) -> None:
        ent = self._highlighted_entry()
        if ent is None:
            return
        if not self._cascade_enabled:
            self._set_status(
                "Cascade is disabled in the Lore Book editor "
                "(no chapters/segments to re-translate)."
            )
            return
        if ent.source_term is None:
            self._set_status(
                "Target-only entries can't trigger a cascade — "
                "the source term is unknown."
            )
            return
        candidates = compute_affected(
            self._project.engine,
            project_id=self._project.project_id,
            entry=ent,
            prev_target_term=ent.target_term,
        )
        if not candidates:
            self._set_status(
                f"No segments need re-translation for {ent.source_term!r}."
            )
            return
        modal = CascadeConfirmScreen(entry=ent, candidates=candidates)
        self.app.push_screen(modal, _make_cascade_callback(self, ent, candidates))

    def cascade_now(
        self,
        entry: GlossaryEntryWithAliases,
        candidates: list[CascadeCandidate],
        confirmed: bool,
    ) -> None:
        if not confirmed:
            self._set_status("Cascade cancelled.")
            return
        self._set_status(f"Re-running cascade on {len(candidates)} segments…")
        self._cascade_worker(entry=entry, candidates=candidates)

    @work(exclusive=True, group="cascade", thread=True)
    def _cascade_worker(
        self,
        *,
        entry: GlossaryEntryWithAliases,
        candidates: list[CascadeCandidate],
    ) -> None:
        try:
            count = cascade_retranslate(
                self._project.engine,
                project_id=self._project.project_id,
                entry=entry,
                prev_target_term=entry.target_term,
                new_target_term=entry.target_term,
                candidates=candidates,
                reason="curator-triggered",
            )
        except Exception as exc:
            self.post_message(CascadeFailed(str(exc)))
            return
        label = entry.source_term or entry.target_term
        self.post_message(CascadeFinished(count=count, source_term=label))

    @on(CascadeFinished)
    def _handle_cascade_finished(self, message: CascadeFinished) -> None:
        self._refresh_entries()
        self._set_status(
            f"Cascade complete: {message.count} segments reverted to pending "
            f"for {message.source_term!r}."
        )

    @on(CascadeFailed)
    def _handle_cascade_failed(self, message: CascadeFailed) -> None:
        self._set_status(f"Cascade failed: {message.error}")

    @on(DataTable.RowHighlighted)
    def _handle_row_highlighted(self, _message: DataTable.RowHighlighted) -> None:
        self._render_detail()


# ---------------------------------------------------------------------------
# Modal dismiss helpers
# ---------------------------------------------------------------------------


def _make_edit_callback(
    screen: GlossaryScreen, entry_id: str
) -> Callable[[_EntryDraftResult | None], None]:
    def _cb(result: _EntryDraftResult | None) -> None:
        screen.apply_edit(entry_id, result)

    return _cb


def _make_delete_callback(
    screen: GlossaryScreen, entry_id: str, source_term: str
) -> Callable[[bool | None], None]:
    def _cb(result: bool | None) -> None:
        screen.apply_delete(entry_id, source_term, bool(result))

    return _cb


def _make_cascade_callback(
    screen: GlossaryScreen,
    entry: GlossaryEntryWithAliases,
    candidates: list[CascadeCandidate],
) -> Callable[[bool | None], None]:
    def _cb(result: bool | None) -> None:
        screen.cascade_now(entry, candidates, bool(result))

    return _cb


def _make_merge_callback(
    screen: GlossaryScreen,
    groups: list[list[GlossaryEntryWithAliases]],
) -> Callable[[int | None], None]:
    def _cb(result: int | None) -> None:
        screen.apply_merge(groups, result)

    return _cb


__all__ = [
    "CascadeConfirmScreen",
    "CascadeFailed",
    "CascadeFinished",
    "DeleteConfirmScreen",
    "EntryEditScreen",
    "GlossaryScreen",
    "MergeDuplicatesScreen",
    "OccurrencesScreen",
]
