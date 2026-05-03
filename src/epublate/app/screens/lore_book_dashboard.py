"""Lore Book dashboard screen.

A focused control center for one Lore Book:

* counts of glossary entries by status / type,
* the list of ePubs already ingested (source / target),
* recent events,
* shortcuts: ``g`` opens the GlossaryScreen on this Lore Book,
  ``i`` ingests a source-language ePub, ``I`` ingests a
  target-language ePub.

This screen mirrors :class:`epublate.app.screens.dashboard.DashboardScreen`
but stays intentionally lean — Lore Books have no chapters /
segments, so the cost meter, batch widget and chapter table aren't
relevant here.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)

from epublate.app.screens.glossary import GlossaryScreen
from epublate.app.widgets import BatchStatusBar
from epublate.db import repo
from epublate.db.schema import LoreSourceKind
from epublate.lore import (
    LoreBook,
    ProjectImportConflict,
    apply_conflict_resolution,
    import_project_glossary,
    list_lore_sources,
)

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Ingest modal
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class IngestRequest:
    """Outcome of :class:`LoreIngestModal`.

    ``kind`` is ``LoreSourceKind.SOURCE`` for the source-language
    extractor pass and ``LoreSourceKind.TARGET`` for the
    target-language pass — the dashboard shows two distinct
    keystrokes for them so the curator picks one explicitly.
    """

    kind: str
    epub_path: Path
    helper_model: str
    max_units: int


class LoreIngestModal(ModalScreen[IngestRequest | None]):
    """Tiny form: ePub path, helper model, max units (segments/chapters)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "Run", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    LoreIngestModal {
        align: center middle;
    }
    LoreIngestModal #lore-ingest-box {
        width: 80%;
        max-width: 90;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    LoreIngestModal Input {
        width: 1fr;
    }
    LoreIngestModal #lore-ingest-help {
        color: $text-muted;
        padding: 1 0 0 0;
    }
    """

    def __init__(
        self,
        *,
        kind: str,
        default_helper_model: str,
        default_max_units: int,
    ) -> None:
        super().__init__()
        self._kind = kind
        self._default_helper_model = default_helper_model
        self._default_max_units = default_max_units

    def compose(self) -> ComposeResult:
        title = (
            "Ingest source-language ePub"
            if self._kind == LoreSourceKind.SOURCE
            else "Ingest target-language ePub"
        )
        unit_label = (
            "Max segments to scan:"
            if self._kind == LoreSourceKind.SOURCE
            else "Max chapters to scan:"
        )
        with Vertical(id="lore-ingest-box"):
            yield Label(f"[b]{title}[/b]", markup=True)
            yield Label("ePub path:")
            yield Input(value="", id="lore-ingest-path", placeholder="/path/to.epub")
            yield Label("Helper model:")
            yield Input(value=self._default_helper_model, id="lore-ingest-model")
            yield Label(unit_label)
            yield Input(
                value=str(self._default_max_units),
                id="lore-ingest-max",
            )
            yield Static(
                "Press [b]Enter[/b] or [b]Ctrl+S[/b] to run, [b]Esc[/b] to cancel.",
                id="lore-ingest-help",
                markup=True,
            )

    def on_mount(self) -> None:
        self.query_one("#lore-ingest-path", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        del event
        self.action_save()

    def action_save(self) -> None:
        raw_path = self.query_one("#lore-ingest-path", Input).value.strip()
        model = self.query_one("#lore-ingest-model", Input).value.strip()
        try:
            max_units = int(
                self.query_one("#lore-ingest-max", Input).value.strip() or "0"
            )
        except ValueError:
            self.app.bell()
            return
        if not raw_path or not model or max_units <= 0:
            self.app.bell()
            return
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            self.app.bell()
            return
        self.dismiss(
            IngestRequest(
                kind=self._kind,
                epub_path=path,
                helper_model=model,
                max_units=max_units,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Import-from-project modals
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ImportProjectRequest:
    """Outcome of :class:`LoreImportProjectModal`."""

    project_dir: Path
    policy: str  # 'collect' | 'skip' | 'overwrite'


class LoreImportProjectModal(ModalScreen[ImportProjectRequest | None]):
    """Pick a project folder and a conflict policy.

    ``collect`` is the default — the dashboard then walks each
    conflict through :class:`LoreConflictResolveModal` so the curator
    decides per-entry. ``skip`` and ``overwrite`` are bulk fast-paths.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "Run", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    LoreImportProjectModal {
        align: center middle;
    }
    LoreImportProjectModal #lore-import-project-box {
        width: 80%;
        max-width: 90;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    LoreImportProjectModal Input {
        width: 1fr;
    }
    LoreImportProjectModal Select {
        width: 1fr;
    }
    LoreImportProjectModal #lore-import-project-help {
        color: $text-muted;
        padding: 1 0 0 0;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="lore-import-project-box"):
            yield Label("[b]Import glossary from a project[/b]", markup=True)
            yield Label("Project folder:")
            yield Input(
                value="",
                id="lore-import-project-path",
                placeholder="/path/to/book1.epublate",
            )
            yield Label("On conflict:")
            yield Select(
                options=[
                    ("Resolve interactively (recommended)", "collect"),
                    ("Skip — keep existing entries", "skip"),
                    ("Overwrite — use incoming entries", "overwrite"),
                ],
                value="collect",
                id="lore-import-project-policy",
                allow_blank=False,
            )
            yield Static(
                "Press [b]Enter[/b] or [b]Ctrl+S[/b] to run, [b]Esc[/b] to cancel.",
                id="lore-import-project-help",
                markup=True,
            )

    def on_mount(self) -> None:
        self.query_one("#lore-import-project-path", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        del event
        self.action_save()

    def action_save(self) -> None:
        raw = self.query_one("#lore-import-project-path", Input).value.strip()
        if not raw:
            self.app.bell()
            return
        path = Path(raw).expanduser()
        if not path.is_dir():
            self.app.bell()
            return
        policy_widget = self.query_one("#lore-import-project-policy", Select)
        policy = str(policy_widget.value or "collect")
        self.dismiss(ImportProjectRequest(project_dir=path.resolve(), policy=policy))

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(slots=True, frozen=True)
class ConflictDecision:
    """Outcome for one conflict in :class:`LoreConflictResolveModal`."""

    action: str  # 'keep_existing' | 'use_incoming' | 'skip' | 'cancel'


class LoreConflictResolveModal(ModalScreen[ConflictDecision | None]):
    """Show one conflict and ask the curator how to resolve it.

    The dashboard pushes one of these per conflict the import collected.
    Ergonomics:

    * ``k`` — keep existing
    * ``u`` — use incoming
    * ``s`` — skip this one
    * ``a`` — abort (stop processing the remaining conflicts)
    * ``Esc`` — same as abort
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("k", "keep", "Keep existing", show=True),
        Binding("u", "use", "Use incoming", show=True),
        Binding("s", "skip", "Skip this one", show=True),
        Binding("a", "abort", "Abort remaining", show=True),
        Binding("escape", "abort", "Abort", show=False),
    ]

    DEFAULT_CSS = """
    LoreConflictResolveModal {
        align: center middle;
    }
    LoreConflictResolveModal #lore-conflict-box {
        width: 90%;
        max-width: 110;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    LoreConflictResolveModal .conflict-side {
        border: round $accent;
        padding: 1 2;
        margin: 0 1 1 0;
        background: $panel;
    }
    LoreConflictResolveModal #lore-conflict-actions {
        padding: 1 0 0 0;
    }
    LoreConflictResolveModal Button {
        margin: 0 1 0 0;
    }
    """

    def __init__(
        self,
        *,
        conflict: ProjectImportConflict,
        index: int,
        total: int,
    ) -> None:
        super().__init__()
        self._conflict = conflict
        self._index = index
        self._total = total

    def compose(self) -> ComposeResult:
        c = self._conflict
        with Vertical(id="lore-conflict-box"):
            yield Label(
                f"[b]Conflict {self._index}/{self._total}[/b]   "
                f"[dim]·[/] [yellow]{c.label}[/yellow]",
                markup=True,
            )
            with Horizontal():
                with Vertical(classes="conflict-side"):
                    yield Label("[b]Existing[/b] (in this Lore Book)", markup=True)
                    yield Static(
                        _format_payload_block(c.existing),
                        markup=True,
                    )
                with Vertical(classes="conflict-side"):
                    yield Label("[b]Incoming[/b] (from project)", markup=True)
                    yield Static(
                        _format_payload_block(c.incoming),
                        markup=True,
                    )
            with Horizontal(id="lore-conflict-actions"):
                yield Button("Keep existing", id="conflict-keep", variant="default")
                yield Button("Use incoming", id="conflict-use", variant="primary")
                yield Button("Skip", id="conflict-skip")
                yield Button("Abort remaining", id="conflict-abort", variant="error")

    @on(Button.Pressed, "#conflict-keep")
    def _keep_pressed(self, event: Button.Pressed) -> None:
        del event
        self.action_keep()

    @on(Button.Pressed, "#conflict-use")
    def _use_pressed(self, event: Button.Pressed) -> None:
        del event
        self.action_use()

    @on(Button.Pressed, "#conflict-skip")
    def _skip_pressed(self, event: Button.Pressed) -> None:
        del event
        self.action_skip()

    @on(Button.Pressed, "#conflict-abort")
    def _abort_pressed(self, event: Button.Pressed) -> None:
        del event
        self.action_abort()

    def action_keep(self) -> None:
        self.dismiss(ConflictDecision(action="keep_existing"))

    def action_use(self) -> None:
        self.dismiss(ConflictDecision(action="use_incoming"))

    def action_skip(self) -> None:
        self.dismiss(ConflictDecision(action="skip"))

    def action_abort(self) -> None:
        self.dismiss(ConflictDecision(action="cancel"))


@dataclass(slots=True)
class _ImportProgress:
    """Mutable progress tracker for an import-from-project flow.

    The conflict-walking flow makes a chain of modal pushes between
    decisions; we keep tallies on this dataclass instead of a dict so
    mypy can see the field types.
    """

    project_name: str
    queue: list[ProjectImportConflict]
    created: int
    updated: int
    skipped: int
    kept: int = 0
    applied: int = 0
    user_skipped: int = 0
    aborted: bool = False


def _format_payload_block(payload: dict[str, object]) -> str:
    """Render a normalized entry payload as Rich markup for the modal."""

    target = payload.get("target_term") or "—"
    status = payload.get("status") or "—"
    type_ = payload.get("type") or "—"
    notes = payload.get("notes") or ""
    src_aliases = payload.get("source_aliases") or []
    tgt_aliases = payload.get("target_aliases") or []

    lines = [
        f"  [b]target:[/b] [cyan]{target}[/cyan]",
        f"  [b]type:[/b] {type_}   [b]status:[/b] {status}",
    ]
    if isinstance(src_aliases, list) and src_aliases:
        lines.append(f"  [b]src aliases:[/b] {', '.join(map(str, src_aliases))}")
    if isinstance(tgt_aliases, list) and tgt_aliases:
        lines.append(f"  [b]tgt aliases:[/b] {', '.join(map(str, tgt_aliases))}")
    if notes:
        lines.append(f"  [dim]notes:[/] {notes}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dashboard screen
# ---------------------------------------------------------------------------


ProviderFactory = Callable[[], object]


class LoreBookDashboardScreen(Screen[None]):
    """Hub screen for an opened Lore Book."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("g", "open_glossary", "Glossary", show=True),
        Binding("i", "ingest_source", "Ingest source", show=True),
        Binding("I", "ingest_target", "Ingest target", show=True),
        Binding("p", "import_project", "Import from project", show=True),
        Binding("S", "open_series", "Series", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("q", "app.pop_screen", "Back", show=True),
        Binding("escape", "app.pop_screen", "Back", show=False),
    ]

    DEFAULT_CSS = """
    LoreBookDashboardScreen #lore-dashboard-body {
        height: 1fr;
    }
    LoreBookDashboardScreen .panel {
        border: round $primary;
        padding: 1 2;
        margin: 0 1 1 1;
        background: $panel;
    }
    LoreBookDashboardScreen .panel-title {
        text-style: bold;
        color: $primary;
    }
    LoreBookDashboardScreen #lore-dashboard-status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $boost;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        lore_book: LoreBook,
        *,
        provider_factory: ProviderFactory | None = None,
        helper_model: str | None = None,
    ) -> None:
        super().__init__()
        self._lore_book = lore_book
        self._provider_factory = provider_factory
        self._helper_model = helper_model
        # Per-import scratch space for the conflict-walking flow
        # (PRD F-LB-10). ``None`` when no import is currently in progress.
        self._import_state: _ImportProgress | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="lore-dashboard-body"):
            yield Static(
                self._header_line(),
                id="lore-dashboard-header",
                classes="panel",
                markup=True,
            )
            with Horizontal():
                with Vertical(classes="dashboard-col"):
                    with Vertical(id="lore-dashboard-stats", classes="panel"):
                        yield Label("Glossary", classes="panel-title")
                        yield Static(
                            "(loading…)",
                            id="lore-dashboard-stats-body",
                            markup=True,
                        )
                    with Vertical(id="lore-dashboard-sources", classes="panel"):
                        yield Label("Ingested ePubs", classes="panel-title")
                        sources_table: DataTable[str] = DataTable(
                            id="lore-dashboard-sources-table",
                            zebra_stripes=True,
                            cursor_type="none",
                        )
                        yield sources_table
                with (
                    Vertical(classes="dashboard-col"),
                    Vertical(id="lore-dashboard-events", classes="panel"),
                ):
                    yield Label("Recent activity", classes="panel-title")
                    yield Static(
                        "(no activity yet)",
                        id="lore-dashboard-events-body",
                        markup=True,
                    )
            yield Static("Ready.", id="lore-dashboard-status", markup=True)
        yield BatchStatusBar(id="lore-dashboard-batch-status")
        yield Footer()

    def on_mount(self) -> None:
        sources = self.query_one("#lore-dashboard-sources-table", DataTable)
        if not sources.columns:
            sources.add_columns("Kind", "ePub", "Entries", "When")
        self._refresh()

    def action_refresh(self) -> None:
        self._refresh()
        self._set_status("Reloaded.")

    def action_open_glossary(self) -> None:
        screen = GlossaryScreen(self._lore_book)
        self.app.push_screen(screen, lambda _r: self._refresh())

    def action_open_series(self) -> None:
        # Imported lazily so the Lore Book screens stay importable even
        # when the recents/Project layer isn't initialized (CLI/test).
        from epublate.app.screens.series import SeriesScreen

        screen = SeriesScreen(self._lore_book)
        self.app.push_screen(screen)

    def action_import_project(self) -> None:
        modal = LoreImportProjectModal()
        self.app.push_screen(modal, self._on_import_project_chosen)

    def action_ingest_source(self) -> None:
        modal = LoreIngestModal(
            kind=LoreSourceKind.SOURCE,
            default_helper_model=self._helper_model or "",
            default_max_units=30,
        )
        self.app.push_screen(modal, self._on_ingest_chosen)

    def action_ingest_target(self) -> None:
        modal = LoreIngestModal(
            kind=LoreSourceKind.TARGET,
            default_helper_model=self._helper_model or "",
            default_max_units=8,
        )
        self.app.push_screen(modal, self._on_ingest_chosen)

    def _on_ingest_chosen(self, request: IngestRequest | None) -> None:
        if request is None:
            self._set_status("Ingest canceled.")
            return
        if self._provider_factory is None:
            # The TUI app is responsible for wiring a provider factory
            # in. When it isn't (CLI context, tests, demos) we stop
            # short with a clear message instead of silently failing.
            self._set_status(
                "No LLM provider configured for this session — "
                "set EPUBLATE_LLM_API_KEY/MODEL or pass --mock-llm."
            )
            return
        provider = self._provider_factory()
        try:
            if request.kind == LoreSourceKind.SOURCE:
                from epublate.lore import ingest_source_epub

                src_summary = ingest_source_epub(
                    self._lore_book,
                    epub_path=request.epub_path,
                    provider=provider,  # type: ignore[arg-type]
                    helper_model=request.helper_model,
                    max_segments=request.max_units,
                )
                self._set_status(
                    f"Source ingest done: {src_summary.proposed_count} proposed."
                )
            else:
                from epublate.lore import ingest_target_epub

                _, tgt_summary = ingest_target_epub(
                    self._lore_book,
                    epub_path=request.epub_path,
                    provider=provider,  # type: ignore[arg-type]
                    helper_model=request.helper_model,
                    max_chapters=request.max_units,
                )
                self._set_status(
                    f"Target ingest done: {tgt_summary.proposed_count} proposed."
                )
        except Exception as exc:
            _logger.exception("lore ingest failed")
            self._set_status(f"Ingest failed: {exc}")
        finally:
            self._refresh()

    # ------------------------------------------------------------------
    # Import-from-project flow
    # ------------------------------------------------------------------

    def _on_import_project_chosen(self, request: ImportProjectRequest | None) -> None:
        if request is None:
            self._set_status("Project import canceled.")
            return
        try:
            summary = import_project_glossary(
                self._lore_book,
                src_project_dir=request.project_dir,
                policy=request.policy,  # type: ignore[arg-type]
            )
        except Exception as exc:
            _logger.exception("lore import-project failed")
            self._set_status(f"Import failed: {exc}")
            self._refresh()
            return

        if not summary.conflicts:
            self._set_status(
                f"Imported from {request.project_dir.name}: "
                f"{summary.created} created, {summary.updated} updated, "
                f"{summary.skipped} skipped."
            )
            self._refresh()
            return

        # Conflicts surfaced — walk them one at a time so the curator
        # can choose per-entry. Tallies live on ``self._import_state``
        # because the chain of modal pushes is async-callback-shaped.
        self._import_state = _ImportProgress(
            project_name=request.project_dir.name,
            queue=list(summary.conflicts),
            created=summary.created,
            updated=summary.updated,
            skipped=summary.skipped,
        )
        self._next_conflict()

    def _next_conflict(self) -> None:
        state = self._import_state
        if state is None or not state.queue:
            self._finalize_import_summary()
            return
        resolved_so_far = state.kept + state.applied + state.user_skipped
        total = len(state.queue) + resolved_so_far
        modal = LoreConflictResolveModal(
            conflict=state.queue[0],
            index=resolved_so_far + 1,
            total=total,
        )
        self.app.push_screen(modal, self._on_conflict_decision)

    def _on_conflict_decision(self, decision: ConflictDecision | None) -> None:
        state = self._import_state
        if state is None or not state.queue:
            return
        if decision is None or decision.action == "cancel":
            # Don't pop — the current conflict is *unresolved*, not
            # decided. Leaving it on the queue lets the summary
            # report a non-zero "remaining" count.
            state.aborted = True
            self._finalize_import_summary()
            return
        action = decision.action
        if action not in ("keep_existing", "use_incoming", "skip"):
            self._set_status(f"Unknown conflict action: {action!r}")
            self._finalize_import_summary()
            return
        conflict = state.queue.pop(0)
        try:
            apply_conflict_resolution(
                self._lore_book,
                conflict=conflict,
                action=action,  # type: ignore[arg-type]
            )
        except Exception as exc:
            _logger.exception("conflict resolution failed for %s", conflict.label)
            self._set_status(f"Resolution failed: {exc}")
            self._finalize_import_summary()
            return
        if action == "keep_existing":
            state.kept += 1
        elif action == "use_incoming":
            state.applied += 1
        else:
            state.user_skipped += 1
        self._next_conflict()

    def _finalize_import_summary(self) -> None:
        state = self._import_state
        if state is None:
            return
        remaining = len(state.queue)
        kept_total = state.kept + state.user_skipped + state.skipped
        msg = (
            f"Imported from {state.project_name}: "
            f"{state.created} created, "
            f"{state.applied + state.updated} updated, "
            f"{kept_total} kept/skipped"
        )
        if state.aborted and remaining:
            msg += f" (aborted with {remaining} conflict(s) unresolved)"
        self._set_status(msg)
        self._import_state = None
        self._refresh()

    # ------------------------------------------------------------------
    # Refresh helpers
    # ------------------------------------------------------------------

    def _header_line(self) -> str:
        book = self._lore_book
        desc = f"\n  [dim]{book.description}[/]" if book.description else ""
        return (
            f"[b]{book.name}[/b]   "
            f"[cyan]{book.source_lang}[/] → [yellow]{book.target_lang}[/]"
            f"   [dim]· path:[/] [b]{book.lore_dir}[/]"
            f"{desc}"
        )

    def _refresh(self) -> None:
        self._refresh_glossary_panel()
        self._refresh_sources_panel()
        self._refresh_events_panel()

    def _refresh_glossary_panel(self) -> None:
        entries = repo.list_glossary_entries(
            self._lore_book.engine, self._lore_book.project_id
        )
        if not entries:
            body = "[dim](empty — ingest an ePub or press [b]g[/] to add manually)[/]"
            self.query_one("#lore-dashboard-stats-body", Static).update(body)
            return
        statuses: Counter[str] = Counter(e.status for e in entries)
        types: Counter[str] = Counter(e.entry.type for e in entries)
        target_only = sum(1 for e in entries if not e.source_known)
        body_lines = [
            f"  [b]{len(entries)}[/] entries total"
            + (f" ([yellow]{target_only}[/] target-only)" if target_only else ""),
            (
                "  [b green]locked[/]: "
                f"{statuses.get('locked', 0)}    "
                f"[b cyan]confirmed[/]: {statuses.get('confirmed', 0)}    "
                f"[b dim]proposed[/]: {statuses.get('proposed', 0)}"
            ),
            "",
            "  [b]Top types:[/]",
        ]
        for kind, count in sorted(types.items(), key=lambda r: (-r[1], r[0]))[:5]:
            body_lines.append(f"    · [b]{kind}[/]: {count}")
        self.query_one("#lore-dashboard-stats-body", Static).update(
            "\n".join(body_lines)
        )

    def _refresh_sources_panel(self) -> None:
        rows = list_lore_sources(
            self._lore_book.engine, project_id=self._lore_book.project_id
        )
        table = self.query_one("#lore-dashboard-sources-table", DataTable)
        table.clear()
        for row in rows:
            short_path = Path(row.epub_path).name or row.epub_path
            table.add_row(
                row.kind,
                short_path,
                str(row.entries_added),
                _format_ts(row.ingested_at),
            )

    def _refresh_events_panel(self) -> None:
        events = repo.list_events(self._lore_book.engine, self._lore_book.project_id)
        if not events:
            self.query_one("#lore-dashboard-events-body", Static).update(
                "[dim](no activity yet)[/]"
            )
            return
        last = events[-10:]
        lines = [
            f"  · [dim]{_format_ts(e.ts)}[/] [b]{e.kind}[/]" for e in reversed(last)
        ]
        self.query_one("#lore-dashboard-events-body", Static).update("\n".join(lines))

    def _set_status(self, message: str) -> None:
        self.query_one("#lore-dashboard-status", Static).update(message)

    @on(DataTable.RowSelected)
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        # The sources table is a passive list; absorb the event so
        # Textual doesn't bubble it up unexpectedly.
        del event


def _format_ts(ts: int) -> str:
    """Compact, locale-free timestamp for tables ('2026-05-02 11:30')."""

    if not ts:
        return "—"
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d %H:%M")


__all__ = [
    "IngestRequest",
    "LoreBookDashboardScreen",
    "LoreIngestModal",
]
