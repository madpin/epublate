"""Intake history screen — browse and annotate helper-LLM intake/pre-pass runs.

Backed by the ``intake_run`` + ``intake_run_entry`` tables (PRD §4.3 /
§7.1). Today's append-only ``event`` log records that an intake or
pre-pass finished but discards the rich roll-up (helper notes,
narrative observations, the list of proposed entries). This screen
surfaces those rows and lets the curator annotate them with
free-form ``curator_notes`` so a future-them remembers why the
"rejected this run's POV" decision happened.

UX contract:

* Left pane / parent screen — :class:`IntakeRunsScreen` — a
  :class:`DataTable` listing runs newest-first. Columns: kind,
  chapter, helper, status, chunks, proposed, cost, when. Pressing
  ``Enter`` (or clicking) opens a detail modal.
* Detail modal — :class:`IntakeRunDetailModal` — renders POV / tense /
  register / audience / suggested style + the helper notes list +
  the linked proposed entries, with an editable ``TextArea`` for
  ``curator_notes`` and a Save button. Action ``s`` applies the
  helper's ``suggested_style_profile`` to the project (calls
  :meth:`Project.update_style`); action ``g`` jumps to the Glossary
  screen so the curator can promote / reject the proposed entries
  the run surfaced.

Keys follow the view-screen / form-modal contract codified in
``.cursor/rules/tui-keybindings.mdc``: ``r`` refreshes, ``q``/``Esc``
back out, ``Enter`` opens the highlighted row.
"""

from __future__ import annotations

import datetime as dt
from typing import ClassVar

from rich.markup import escape as escape_markup
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    Static,
    TextArea,
)

from epublate.app.widgets import BatchStatusBar
from epublate.core.project import Project
from epublate.core.style import label_for as style_label_for
from epublate.db import repo
from epublate.db.schema import IntakeRunKind


class IntakeRunDetailModal(ModalScreen[bool]):
    """Read + edit one ``intake_run`` row.

    Returns ``True`` when the curator saved an edit (so the caller
    knows to refresh the list) or ``False`` when they backed out
    without changes. The modal does not expose suggested-style /
    Glossary jumps as buttons — they live behind dedicated bindings
    (``s`` / ``g``) so the verb-key vocabulary stays consistent
    with the rest of the TUI.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "Save", show=True),
        Binding("s", "apply_style", "Apply tone", show=True),
        Binding("g", "open_glossary", "Glossary", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    IntakeRunDetailModal {
        align: center middle;
    }
    IntakeRunDetailModal #intake-detail-box {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 90%;
        border: round $accent;
        padding: 1 2;
        background: $panel;
    }
    IntakeRunDetailModal #intake-detail-title {
        text-style: bold;
        color: $primary;
        padding: 0 0 1 0;
    }
    IntakeRunDetailModal .summary-block {
        height: auto;
        margin: 0 0 1 0;
        padding: 1 1;
        border: round $primary 50%;
        background: $boost;
    }
    IntakeRunDetailModal TextArea {
        height: 8;
        background: $surface;
        border: tall $primary 30%;
    }
    IntakeRunDetailModal TextArea:focus {
        border: tall $accent;
    }
    IntakeRunDetailModal #intake-detail-buttons {
        height: 3;
        padding: 1 0 0 0;
        align-horizontal: right;
    }
    IntakeRunDetailModal #intake-detail-buttons Button {
        margin: 0 0 0 1;
    }
    IntakeRunDetailModal #intake-detail-status {
        height: 1;
        color: $text-muted;
        padding: 0;
    }
    """

    def __init__(self, project: Project, run: repo.IntakeRunRow) -> None:
        super().__init__()
        self._project = project
        self._run = run
        self._initial_curator_notes = run.curator_notes or ""
        self._saved = False

    def compose(self) -> ComposeResult:
        with Vertical(id="intake-detail-box"):
            yield Label(self._title_text(), id="intake-detail-title", markup=True)
            with VerticalScroll():
                yield Static(
                    self._summary_text(),
                    classes="summary-block",
                    markup=True,
                )
                if self._run.notes:
                    yield Static(
                        self._notes_text(),
                        classes="summary-block",
                        markup=True,
                    )
                yield Static(
                    self._proposed_text(),
                    classes="summary-block",
                    markup=True,
                )
            yield Label("Curator notes (free-form):")
            yield TextArea(
                text=self._initial_curator_notes,
                id="intake-detail-notes",
            )
            yield Static(
                "[dim]Ctrl+S to save · S to apply suggested tone · "
                "G to open Glossary · Esc to close.[/dim]",
                id="intake-detail-status",
                markup=True,
            )
            with Horizontal(id="intake-detail-buttons"):
                yield Button("Cancel", id="intake-detail-cancel")
                yield Button(
                    "Save notes",
                    id="intake-detail-save",
                    variant="primary",
                )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#intake-detail-notes", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "intake-detail-save":
            self.action_save()
        elif bid == "intake-detail-cancel":
            self.action_cancel()

    def action_save(self) -> None:
        text_widget = self.query_one("#intake-detail-notes", TextArea)
        try:
            repo.update_intake_run_curator_notes(
                self._project.engine,
                intake_run_id=self._run.id,
                curator_notes=text_widget.text,
            )
        except ValueError as exc:
            self.query_one("#intake-detail-status", Static).update(
                f"[red]Could not save: {exc}[/red]"
            )
            return
        self._saved = True
        self.dismiss(True)

    def action_apply_style(self) -> None:
        profile = self._run.suggested_style_profile
        if not profile:
            self.query_one("#intake-detail-status", Static).update(
                "[dim]No suggested tone to apply.[/dim]"
            )
            return
        try:
            self._project.update_style(style_profile=profile, custom_text=None)
        except (ValueError, OSError) as exc:
            self.query_one("#intake-detail-status", Static).update(
                f"[red]Could not apply tone: {exc}[/red]"
            )
            return
        self.query_one("#intake-detail-status", Static).update(
            f"[green]Tone applied: {style_label_for(profile)}.[/green]"
        )

    def action_open_glossary(self) -> None:
        # Lazy import to mirror the rest of the cross-screen jumps in
        # the codebase (avoids a hard import cycle on Textual startup).
        from epublate.app.screens.glossary import GlossaryScreen

        screen = GlossaryScreen(self._project)
        # The Glossary screen does not (yet) accept a "highlight these
        # entry ids" filter; opening it is the discoverable next step
        # for the curator who just reviewed a pre-pass.
        self.app.push_screen(screen)
        self.dismiss(self._saved)

    def action_cancel(self) -> None:
        self.dismiss(self._saved)

    def _title_text(self) -> str:
        kind_label = (
            "Book intake"
            if self._run.kind == IntakeRunKind.BOOK_INTAKE
            else "Chapter pre-pass"
        )
        return f"[b]{kind_label}[/b] · {self._run.helper_model}"

    def _summary_text(self) -> str:
        when = _fmt_ts(self._run.started_at)
        duration = max(0, self._run.finished_at - self._run.started_at)
        chapter_label = self._chapter_label()
        suggested = (
            f"{style_label_for(self._run.suggested_style_profile)} "
            f"({self._run.suggested_style_profile})"
            if self._run.suggested_style_profile
            else "(none)"
        )
        lines = [
            f"  status         : {escape_markup(self._run.status)}",
            f"  chapter        : {escape_markup(chapter_label)}",
            f"  when           : {when} · {duration}s",
            f"  chunks         : {self._run.chunks} "
            f"({self._run.cached_chunks} cached, "
            f"{self._run.failed_chunks} failed)",
            f"  proposed       : {self._run.proposed_count}",
            f"  tokens         : {self._run.prompt_tokens} prompt + "
            f"{self._run.completion_tokens} completion",
            f"  cost           : ${self._run.cost_usd:.4f}",
            f"  POV / tense    : {self._run.pov or '(unset)'} / "
            f"{self._run.tense or '(unset)'}",
            f"  register       : {self._run.narrative_register or '(unset)'}",
            f"  audience       : {self._run.audience or '(unset)'}",
            f"  suggested tone : {suggested}",
        ]
        if self._run.error:
            lines.append(f"  error          : {escape_markup(self._run.error)}")
        return "\n".join(lines)

    def _notes_text(self) -> str:
        bullets = "\n".join(f"  - {escape_markup(note)}" for note in self._run.notes)
        return f"[b]Helper notes[/b]\n{bullets}"

    def _proposed_text(self) -> str:
        if not self._run.proposed_entry_ids:
            return "[b]Proposed entries[/b]\n  (none surfaced by this run)"
        # Resolve the entry rows so the curator sees the *current*
        # status — promoted entries are no longer ``proposed`` and
        # the screen should make that obvious.
        seen: list[str] = []
        for entry_id in self._run.proposed_entry_ids:
            entry = repo.get_glossary_entry(self._project.engine, entry_id)
            if entry is None:
                seen.append(f"  - [dim](deleted entry {entry_id[:8]})[/dim]")
                continue
            label = (
                entry.source_term
                if entry.source_term is not None
                else f"(target-only) {entry.target_term}"
            )
            seen.append(
                f"  - [{escape_markup(entry.entry.type)}] "
                f"{escape_markup(label)} → {escape_markup(entry.target_term)} "
                f"[dim]({entry.status})[/dim]"
            )
        return "[b]Proposed entries[/b]\n" + "\n".join(seen)

    def _chapter_label(self) -> str:
        if self._run.chapter_id is None:
            return "(whole book)"
        chapter = next(
            (
                c
                for c in repo.list_chapters(
                    self._project.engine, self._project.project_id
                )
                if c.id == self._run.chapter_id
            ),
            None,
        )
        if chapter is None:
            return f"(missing chapter {self._run.chapter_id[:8]})"
        return chapter.title or chapter.href or f"chapter {chapter.spine_idx + 1}"


class IntakeRunsScreen(Screen[None]):
    """Browse helper-LLM intake / pre-pass runs (PRD §4.3 / §7.1)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "open_detail", "Open", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("q", "app.pop_screen", "Back", show=True),
        Binding("escape", "app.pop_screen", "Back", show=False),
    ]

    DEFAULT_CSS = """
    IntakeRunsScreen #intake-runs-body {
        height: 1fr;
        padding: 0 1 0 1;
    }
    IntakeRunsScreen #intake-runs-table {
        height: 1fr;
        border: round $primary;
    }
    IntakeRunsScreen #intake-runs-status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    """

    def __init__(self, project: Project) -> None:
        super().__init__()
        self._project = project
        self._rows: list[repo.IntakeRunRow] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="intake-runs-body"):
            table: DataTable[str] = DataTable(
                id="intake-runs-table", zebra_stripes=True, cursor_type="row"
            )
            table.add_columns(
                "Kind",
                "Chapter",
                "Helper",
                "Status",
                "Chunks",
                "Proposed",
                "Cost",
                "When",
            )
            yield table
            yield Static(
                "Press Enter to read or annotate a run.",
                id="intake-runs-status",
            )
        yield BatchStatusBar(id="intake-runs-batch-status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def action_refresh(self) -> None:
        self._refresh()

    def _refresh(self) -> None:
        self._rows = repo.list_intake_runs(
            self._project.engine, project_id=self._project.project_id
        )
        chapter_lookup = {
            c.id: (c.title or c.href or f"chapter {c.spine_idx + 1}")
            for c in repo.list_chapters(self._project.engine, self._project.project_id)
        }
        table = self.query_one("#intake-runs-table", DataTable)
        table.clear()
        for row in self._rows:
            kind_label = "book" if row.kind == IntakeRunKind.BOOK_INTAKE else "chapter"
            chapter_label = (
                "—"
                if row.chapter_id is None
                else escape_markup(
                    chapter_lookup.get(row.chapter_id, row.chapter_id[:8])
                )
            )
            table.add_row(
                kind_label,
                chapter_label,
                escape_markup(row.helper_model),
                escape_markup(row.status),
                str(row.chunks),
                str(row.proposed_count),
                f"${row.cost_usd:.4f}",
                _fmt_ts(row.started_at),
                key=row.id,
            )
        self._set_status(
            f"{len(self._rows)} run(s) recorded — newest first."
            if self._rows
            else "No intake runs yet. Run the Dashboard's intake (E) "
            "or a batch with pre-pass on."
        )

    def _set_status(self, text: str) -> None:
        self.query_one("#intake-runs-status", Static).update(text)

    def _highlighted_row(self) -> repo.IntakeRunRow | None:
        table = self.query_one("#intake-runs-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        rid = str(row_key.value) if row_key.value is not None else None
        if rid is None:
            return None
        return next((r for r in self._rows if r.id == rid), None)

    def action_open_detail(self) -> None:
        self._open_detail()

    @on(DataTable.RowSelected)
    def _on_row_selected(self, message: DataTable.RowSelected) -> None:
        del message
        self._open_detail()

    def _open_detail(self) -> None:
        row = self._highlighted_row()
        if row is None:
            return
        modal = IntakeRunDetailModal(self._project, row)
        self.app.push_screen(modal, self._after_detail_closed)

    def _after_detail_closed(self, saved: bool | None) -> None:
        if saved:
            self._refresh()
            self._set_status("Curator notes saved.")


def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return ""
    try:
        return dt.datetime.fromtimestamp(ts, tz=dt.UTC).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(ts)


__all__ = ["IntakeRunDetailModal", "IntakeRunsScreen"]
