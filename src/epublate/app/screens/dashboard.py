"""Project Dashboard — landing screen for an open project (PRD §4.6 / M4).

The Dashboard is the curator's hub: it reports progress, running cost,
inbox depth, and the last few project events, and dispatches every
heavy operation (batch run, budget edit, export, navigate to Reader/
Glossary/Inbox).

Wiring:

* ``epublate open <project>`` lands here (see :mod:`epublate.cli`).
* The screen never blocks the UI: stats are computed in a worker, and
  batch runs go through ``core.batch.run_batch`` on a separate worker
  that posts :class:`BatchTick` messages back to the main loop.
* Exports go through ``Project.export`` on a dedicated worker so the
  curator can save a (possibly partial) ePub at any point — even
  while a batch is still chewing through pending segments
  (PRD §7.6 / F-IO-7).
* All DB writes go through the repo layer (TUI rule §4).

The Reader / Glossary / Inbox screens are pushed by bindings; popping
them returns the curator here.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from rich.markup import escape as escape_markup
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Input, Label, Static

from epublate.app.branding import ICON_ARROW
from epublate.app.widgets import BatchProgressMeter, BatchSnapshot, CostMeter
from epublate.core.batch import (
    BatchOptions,
    BatchPaused,
    BatchProgressEvent,
    BatchSummary,
    run_batch,
)
from epublate.core.extractor import (
    DEFAULT_INTAKE_MAX_SEGMENTS,
    IntakeOptions,
    IntakeSummary,
    run_book_intake,
)
from epublate.core.project import Project
from epublate.core.stats import (
    ChapterShapeSummary,
    ProjectStats,
    compute_chapter_shapes,
    compute_stats,
    recent_alerts,
    summarize_chapter_shapes,
)
from epublate.db import repo
from epublate.llm.base import LLMProvider
from epublate.llm.factory import build_provider, resolve_helper_model

_logger = logging.getLogger(__name__)

ProviderFactory = Callable[[], LLMProvider]


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class BatchRequest:
    """Form result from :class:`BatchModal`."""

    chapters: str
    concurrency: int
    model: str
    budget_usd: float | None
    bypass_cache: bool
    # Small-segment grouping: when on, dense list-like content (TOCs,
    # indices, glossaries) is translated in batched LLM calls instead
    # of one round-trip per segment. See ``BatchOptions`` for details.
    group_small_segments: bool = True
    group_max_items: int = 50


class BatchModal(ModalScreen[BatchRequest | None]):
    """Configure a batch run before dispatching.

    The form is intentionally small — chapter range, concurrency, model,
    optional budget override. Nothing here writes to the DB; the
    Dashboard's worker owns that.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "submit", "Start", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    BatchModal {
        align: center middle;
    }
    BatchModal #batch-box {
        width: 80%;
        height: auto;
        max-height: 90%;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    BatchModal #batch-stats {
        height: auto;
        padding: 0 1;
        margin: 0 0 1 0;
        border: round $accent;
        background: $boost;
    }
    BatchModal .row {
        height: auto;
        padding: 0 0 1 0;
    }
    BatchModal .row Label {
        width: 28;
    }
    BatchModal Input {
        width: 1fr;
    }
    BatchModal #batch-help {
        height: auto;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        default_model: str,
        default_concurrency: int = 1,
        default_budget: float | None = None,
        chapter_shape_summary: ChapterShapeSummary | None = None,
        project_stats: ProjectStats | None = None,
    ) -> None:
        super().__init__()
        self._default_model = default_model
        self._default_concurrency = default_concurrency
        self._default_budget = default_budget
        self._chapter_shape_summary = chapter_shape_summary
        self._project_stats = project_stats

    def compose(self) -> ComposeResult:
        with Vertical(id="batch-box"):
            yield Label("[b]Batch translate[/b]", markup=True)
            yield Static(
                self._stats_text(),
                id="batch-stats",
                markup=True,
            )
            with Horizontal(classes="row"):
                yield Label("Chapters (e.g. 1-3, *):")
                yield Input(value="*", id="batch-chapters")
            with Horizontal(classes="row"):
                yield Label("Concurrency:")
                yield Input(
                    value=str(self._default_concurrency),
                    id="batch-concurrency",
                )
            with Horizontal(classes="row"):
                yield Label("Model:")
                yield Input(value=self._default_model, id="batch-model")
            with Horizontal(classes="row"):
                yield Label("Budget USD (blank = use project):")
                yield Input(
                    value=(
                        f"{self._default_budget:.4f}"
                        if self._default_budget is not None
                        else ""
                    ),
                    id="batch-budget",
                )
            with Horizontal(classes="row"):
                yield Label("Group small segments [y/n]:")
                yield Input(value="y", id="batch-group")
            with Horizontal(classes="row"):
                yield Label("Group max items (1-500):")
                yield Input(value="50", id="batch-group-size")
            yield Static(
                "Press [b]Ctrl+S[/b] to start, [b]Escape[/b] to cancel. "
                "Use [b]*[/b] for all chapters, [b]N[/b] for one, "
                "[b]A-B[/b] for a 1-indexed range over translatable chapters. "
                "Grouping batches short placeholder-free segments (TOCs, indices, "
                "lists) into one LLM call to cut cost + latency. "
                "Open the [b]Reader[/b] (key [b]o[/b]) once the batch starts "
                "to watch segments translate live.",
                id="batch-help",
                markup=True,
            )

    def _stats_text(self) -> str:
        summary = self._chapter_shape_summary
        stats = self._project_stats
        if summary is None:
            return "[dim](no project stats available)[/dim]"
        lines: list[str] = ["[b]Project stats[/b]"]
        lines.append(
            f"  Chapters       : {summary.translatable_chapters} translatable "
            f"of {summary.total_chapters} total"
        )
        lines.append(
            f"  Segments       : {summary.total_segments} translatable"
            + (
                f" — {stats.translated_count}/{stats.segment_count} translated, "
                f"{stats.approved_count} approved, {stats.flagged_count} flagged"
                if stats is not None
                else ""
            )
        )
        if stats is not None and stats.segment_count:
            pct = stats.translated_count * 100.0 / stats.segment_count
            pending = stats.segment_count - stats.translated_count
            lines.append(f"  Remaining      : {pending} pending ({100.0 - pct:5.1f}%)")
        if summary.longest is not None:
            longest_title = (
                summary.longest.title
                or summary.longest.href
                or f"chapter {summary.longest.spine_idx + 1}"
            )
            lines.append(
                f"  Longest chapter: {summary.longest.translatable_count} segs — "
                f"[b]{escape_markup(longest_title)}[/b]"
            )
        if summary.shortest is not None and summary.translatable_chapters > 1:
            shortest_title = (
                summary.shortest.title
                or summary.shortest.href
                or f"chapter {summary.shortest.spine_idx + 1}"
            )
            lines.append(
                f"  Shortest       : {summary.shortest.translatable_count} segs — "
                f"{escape_markup(shortest_title)}"
            )
        if summary.translatable_chapters:
            lines.append(
                f"  Avg / median   : "
                f"{summary.average_segments:.1f} / {summary.median_segments:.1f} segs"
            )
        if stats is not None and stats.spend_usd:
            lines.append(
                f"  Spend so far   : ${stats.spend_usd:.4f}"
                + (
                    f" of ${stats.budget_usd:.4f} budget"
                    if stats.budget_usd is not None
                    else " (no cap)"
                )
            )
        return "\n".join(lines)

    def on_mount(self) -> None:
        self.query_one("#batch-chapters", Input).focus()

    def action_submit(self) -> None:
        chapters = self.query_one("#batch-chapters", Input).value.strip() or "*"
        concurrency_raw = self.query_one("#batch-concurrency", Input).value.strip()
        model = self.query_one("#batch-model", Input).value.strip()
        budget_raw = self.query_one("#batch-budget", Input).value.strip()
        group_raw = self.query_one("#batch-group", Input).value.strip().lower()
        group_size_raw = self.query_one("#batch-group-size", Input).value.strip()

        try:
            concurrency = max(1, int(concurrency_raw or "1"))
        except ValueError:
            self.app.bell()
            return
        if not model:
            self.app.bell()
            return
        budget: float | None
        if not budget_raw:
            budget = None
        else:
            try:
                budget = float(budget_raw)
            except ValueError:
                self.app.bell()
                return
            if budget < 0:
                self.app.bell()
                return

        group_enabled = group_raw in {"", "y", "yes", "true", "1", "on"}
        try:
            group_size = int(group_size_raw or "50")
        except ValueError:
            self.app.bell()
            return
        group_size = max(1, min(500, group_size))

        self.dismiss(
            BatchRequest(
                chapters=chapters,
                concurrency=concurrency,
                model=model,
                budget_usd=budget,
                bypass_cache=False,
                group_small_segments=group_enabled,
                group_max_items=group_size,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(slots=True, frozen=True)
class BudgetRequest:
    """Form result from :class:`BudgetModal`."""

    budget_usd: float | None  # ``None`` clears the cap


class BudgetModal(ModalScreen[BudgetRequest | None]):
    """Set or clear the project's USD budget cap (PRD F-LLM-8)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "submit", "Save", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    BudgetModal {
        align: center middle;
    }
    BudgetModal #budget-box {
        width: 60%;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    BudgetModal .row {
        height: auto;
        padding: 0 0 1 0;
    }
    BudgetModal .row Label {
        width: 22;
    }
    BudgetModal Input {
        width: 1fr;
    }
    """

    def __init__(self, *, current_budget: float | None) -> None:
        super().__init__()
        self._current_budget = current_budget

    def compose(self) -> ComposeResult:
        with Vertical(id="budget-box"):
            yield Label("Set or clear the project budget cap")
            with Horizontal(classes="row"):
                yield Label("Budget USD (blank to clear):")
                yield Input(
                    value=(
                        f"{self._current_budget:.4f}"
                        if self._current_budget is not None
                        else ""
                    ),
                    id="budget-input",
                )
            yield Static(
                "Ctrl+S to save, Escape to cancel.",
                markup=False,
            )

    def on_mount(self) -> None:
        self.query_one("#budget-input", Input).focus()

    def action_submit(self) -> None:
        raw = self.query_one("#budget-input", Input).value.strip()
        if not raw:
            self.dismiss(BudgetRequest(budget_usd=None))
            return
        try:
            value = float(raw)
        except ValueError:
            self.app.bell()
            return
        if value < 0:
            self.app.bell()
            return
        self.dismiss(BudgetRequest(budget_usd=value))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Intake modal (M5)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class IntakeRequest:
    """Form result from :class:`IntakeModal`."""

    helper_model: str
    max_segments: int


class IntakeModal(ModalScreen[IntakeRequest | None]):
    """Configure the helper-LLM book intake pass (PRD §7.1 / M5)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "submit", "Start", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    IntakeModal {
        align: center middle;
    }
    IntakeModal #intake-box {
        width: 60%;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    IntakeModal .row {
        height: auto;
        padding: 0 0 1 0;
    }
    IntakeModal .row Label {
        width: 22;
    }
    IntakeModal Input {
        width: 1fr;
    }
    IntakeModal #intake-help {
        height: auto;
        color: $text-muted;
    }
    """

    def __init__(
        self,
        *,
        default_helper_model: str,
        default_max_segments: int = DEFAULT_INTAKE_MAX_SEGMENTS,
    ) -> None:
        super().__init__()
        self._default_helper_model = default_helper_model
        self._default_max_segments = default_max_segments

    def compose(self) -> ComposeResult:
        with Vertical(id="intake-box"):
            yield Label("Book intake")
            with Horizontal(classes="row"):
                yield Label("Helper model:")
                yield Input(value=self._default_helper_model, id="intake-helper")
            with Horizontal(classes="row"):
                yield Label("Max segments:")
                yield Input(
                    value=str(self._default_max_segments),
                    id="intake-max",
                )
            yield Static(
                "Press [b]Ctrl+S[/b] to start, [b]Escape[/b] to cancel. "
                "Helper LLM scans the first N segments and proposes "
                "characters / places / terms.",
                id="intake-help",
                markup=True,
            )

    def on_mount(self) -> None:
        self.query_one("#intake-helper", Input).focus()

    def action_submit(self) -> None:
        helper = self.query_one("#intake-helper", Input).value.strip()
        max_raw = self.query_one("#intake-max", Input).value.strip()
        if not helper:
            self.app.bell()
            return
        try:
            max_seg = max(1, int(max_raw or str(DEFAULT_INTAKE_MAX_SEGMENTS)))
        except ValueError:
            self.app.bell()
            return
        self.dismiss(IntakeRequest(helper_model=helper, max_segments=max_seg))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Export modal (PRD §7.6 / F-IO-7)
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ExportRequest:
    """Form result from :class:`ExportModal`.

    ``epubcheck`` mirrors the CLI flag — it's only honored when the
    optional ``[epubcheck]`` extra is installed; otherwise the export
    succeeds and the validator simply reports ``skipped`` (PRD F-IO-6).
    """

    out_path: Path
    epubcheck: bool = False


def default_export_path(project: Project) -> Path:
    """Pick a sensible default location for the exported ePub.

    Lands the file next to ``original.epub`` so the curator finds it in
    the same folder they opened. The stem reuses the project's DB stem
    (which itself came from the source ePub's stem) and tags the
    target language so multiple translations of the same book don't
    collide.
    """

    stem = project.db_path.stem or "translated"
    target = (project.target_lang or "translated").lower()
    return project.project_dir / f"{stem}.{target}.epub"


class ExportModal(ModalScreen[ExportRequest | None]):
    """Save the (possibly partial) translated ePub from inside the TUI.

    The modal is intentionally small: a single path field with a
    "Browse…" button to pick the output folder, an opt-in epubcheck
    toggle, and an explicit reminder that the export works at any
    point in the project's lifecycle (untranslated segments fall
    back to the source per PRD F-IO-7).
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "submit", "Save", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+o", "browse", "Browse", show=True),
    ]

    DEFAULT_CSS = """
    ExportModal {
        align: center middle;
        background: $background 60%;
    }
    ExportModal #export-box {
        width: 80%;
        max-width: 110;
        height: auto;
        border: round $accent;
        padding: 1 2;
        background: $panel;
    }
    ExportModal #export-title {
        text-style: bold;
        color: $primary;
        padding: 0;
    }
    ExportModal #export-subtitle {
        color: $text-muted;
        padding: 0 0 1 0;
        height: 1;
    }
    ExportModal .field-row {
        height: auto;
        padding: 0;
        margin: 0 0 1 0;
    }
    ExportModal .field-label {
        width: 14;
        padding: 1 1 0 0;
        color: $secondary;
        text-style: bold;
    }
    ExportModal .field-stack {
        width: 1fr;
        height: auto;
    }
    ExportModal .field-input-row {
        height: auto;
    }
    ExportModal .field-input-row Input {
        width: 1fr;
    }
    ExportModal .field-input-row Button {
        width: 12;
        height: 3;
        margin: 0 0 0 1;
    }
    ExportModal .field-hint {
        color: $text-muted;
        padding: 0 0 0 1;
        height: auto;
    }
    ExportModal #export-info {
        color: $text;
        padding: 1 1;
        margin: 0 0 1 0;
        border: round $accent;
        background: $boost;
    }
    ExportModal #export-error {
        color: $error;
        text-style: bold;
        padding: 1 0 0 0;
        height: auto;
    }
    ExportModal #export-buttons {
        height: 3;
        padding: 1 0 0 0;
        align-horizontal: right;
    }
    ExportModal #export-buttons Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(
        self,
        *,
        default_path: Path,
        project_stats: ProjectStats | None = None,
        epubcheck: bool = False,
    ) -> None:
        super().__init__()
        self._default_path = default_path
        self._project_stats = project_stats
        self._initial_epubcheck = epubcheck

    def compose(self) -> ComposeResult:
        with Vertical(id="export-box"):
            yield Label(
                f"  {ICON_ARROW} Save translated ePub",
                id="export-title",
            )
            yield Label(
                "Write the (possibly partial) translation to disk.",
                id="export-subtitle",
            )
            yield Static(self._info_text(), id="export-info", markup=True)
            with Horizontal(classes="field-row"):
                yield Label("Output file", classes="field-label")
                with Vertical(classes="field-stack"):
                    with Horizontal(classes="field-input-row"):
                        yield Input(
                            value=str(self._default_path),
                            placeholder="/path/to/translated.epub",
                            id="export-path",
                        )
                        yield Button(
                            "Browse…",
                            id="export-path-browse",
                            variant="default",
                        )
                    yield Static(
                        "press [b]Ctrl+O[/b] (or [b]Browse[/b]) to pick a folder; "
                        "the filename is editable",
                        classes="field-hint",
                        markup=True,
                    )
            with Horizontal(classes="field-row"):
                yield Label("Validate", classes="field-label")
                with Vertical(classes="field-stack"):
                    yield Input(
                        value="y" if self._initial_epubcheck else "n",
                        id="export-epubcheck",
                    )
                    yield Static(
                        "[b]y[/b] = run epubcheck after the write "
                        "(needs the optional [b][[epubcheck]][/] extra; "
                        "skipped otherwise). [b]n[/b] = skip.",
                        classes="field-hint",
                        markup=True,
                    )
            yield Static("", id="export-error", markup=False)
            with Horizontal(id="export-buttons"):
                yield Button("Cancel", id="export-cancel", variant="default")
                yield Button(
                    "Save ePub",
                    id="export-submit",
                    variant="primary",
                )
        yield Footer()

    def _info_text(self) -> str:
        stats = self._project_stats
        if stats is None or stats.segment_count == 0:
            head = (
                "[b]Save anytime.[/b] Untranslated segments fall back to the "
                "[b]source text[/b] so the file is always valid and re-readable."
            )
            return head + "\n  Press [b]Ctrl+S[/b] to save · [b]Esc[/b] to cancel."
        translated = stats.translated_count
        total = stats.segment_count
        pct = stats.progress_ratio * 100.0
        pending = total - translated
        return (
            "[b]Save anytime.[/b] Untranslated segments fall back to the "
            "[b]source text[/b] so the file is always valid and re-readable.\n"
            f"  Translated: [b]{translated}/{total}[/b] "
            f"({pct:5.1f}%) · pending: [b]{pending}[/b]\n"
            "  Press [b]Ctrl+S[/b] to save · [b]Esc[/b] to cancel."
        )

    def on_mount(self) -> None:
        self.query_one("#export-path", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "export-submit":
            self.action_submit()
            return
        if button_id == "export-cancel":
            self.action_cancel()
            return
        if button_id == "export-path-browse":
            self.action_browse()
            return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        del event
        self.action_submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_browse(self) -> None:
        # Imported lazily to avoid a circular import at module load —
        # the file browser already imports app-level helpers.
        from epublate.app.screens.file_browser import FileBrowserModal

        raw = self.query_one("#export-path", Input).value.strip()
        start: Path | None = None
        if raw:
            candidate = Path(raw).expanduser()
            if candidate.is_dir():
                start = candidate
            elif candidate.parent.is_dir():
                start = candidate.parent
        if start is None:
            start = self._default_path.parent
        modal = FileBrowserModal(
            mode="directory",
            start_path=start,
            title=f"  {ICON_ARROW} Pick output folder",
            subtitle=(
                "Navigate to the folder you want the ePub saved into, "
                "then press 'Pick this folder'."
            ),
        )
        self.app.push_screen(modal, self._on_folder_picked)

    def _on_folder_picked(self, result: Path | None) -> None:
        if result is None:
            return
        # Preserve the filename the user already had typed; only the
        # parent directory changes. The default filename is reused
        # when the existing input is empty.
        existing = self.query_one("#export-path", Input).value.strip()
        if existing:
            filename = Path(existing).name or self._default_path.name
        else:
            filename = self._default_path.name
        self.query_one("#export-path", Input).value = str(result / filename)

    def action_submit(self) -> None:
        raw = self.query_one("#export-path", Input).value.strip()
        if not raw:
            self._set_error("Output path is required.")
            return
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        else:
            path = path.resolve()
        if path.suffix.lower() != ".epub":
            self._set_error(f"Output must have an .epub suffix (got {path.suffix!r}).")
            return
        if path.is_dir():
            self._set_error(f"Output path is a directory: {path}")
            return
        parent = path.parent
        if not parent.exists():
            self._set_error(f"Parent folder does not exist: {parent}")
            return
        epubcheck_raw = self.query_one("#export-epubcheck", Input).value.strip().lower()
        epubcheck = epubcheck_raw in {"y", "yes", "true", "1", "on"}
        self.dismiss(ExportRequest(out_path=path, epubcheck=epubcheck))

    def _set_error(self, message: str) -> None:
        self.query_one("#export-error", Static).update(message)


# ---------------------------------------------------------------------------
# Messages emitted by background workers
# ---------------------------------------------------------------------------


class BatchTick(Message):
    """One per-segment progress tick, posted from the batch worker."""

    def __init__(self, event: BatchProgressEvent) -> None:
        super().__init__()
        self.event = event


class BatchFinished(Message):
    """Batch completed (or paused) successfully without crashing the worker."""

    def __init__(self, summary: BatchSummary, *, paused: bool) -> None:
        super().__init__()
        self.summary = summary
        self.paused = paused


class BatchFailed(Message):
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


class IntakeFinished(Message):
    """Helper-LLM intake completed successfully."""

    def __init__(self, summary: IntakeSummary) -> None:
        super().__init__()
        self.summary = summary


class IntakeFailed(Message):
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


class ExportFinished(Message):
    """Export worker wrote the ePub successfully."""

    def __init__(
        self,
        *,
        out_path: Path,
        epubcheck_summary: str | None,
        epubcheck_status: str | None,
    ) -> None:
        super().__init__()
        self.out_path = out_path
        self.epubcheck_summary = epubcheck_summary
        self.epubcheck_status = epubcheck_status


class ExportFailed(Message):
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


# ---------------------------------------------------------------------------
# Screen
# ---------------------------------------------------------------------------


class DashboardScreen(Screen[None]):
    """Project Dashboard (PRD §4.6 / M4 landing screen)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("o", "open_reader", "Reader", show=True),
        Binding("g", "open_glossary", "Glossary", show=True),
        Binding("i", "open_inbox", "Inbox", show=True),
        Binding("b", "batch", "Batch", show=True),
        # ``x`` = eXport. Save the (possibly partial) translated ePub
        # to disk at any point in the project's lifecycle (PRD §7.6 /
        # F-IO-7); untranslated segments fall back to source text so
        # the file is always valid and re-readable.
        Binding("x", "export", "Save ePub", show=True),
        Binding("B", "set_budget", "Budget", show=True),
        Binding("e", "intake", "Intake", show=True),
        Binding("s", "open_settings", "Settings", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("q", "app.pop_screen", "Back", show=True),
        # ``escape`` mirrors ``q`` so curators can back out with the
        # universal "cancel" key; hidden from the footer to keep the
        # binding bar concise.
        Binding("escape", "app.pop_screen", "Back", show=False),
    ]

    DEFAULT_CSS = """
    DashboardScreen #dashboard-body {
        height: 1fr;
    }
    DashboardScreen .panel {
        border: round $primary;
        padding: 1 2;
        margin: 0 0 1 0;
    }
    DashboardScreen #dashboard-progress {
        height: auto;
    }
    DashboardScreen #dashboard-cost {
        height: auto;
    }
    DashboardScreen #dashboard-inbox-digest {
        height: auto;
    }
    DashboardScreen #dashboard-batch-panel {
        display: none;
        height: auto;
        margin: 0 0 1 0;
        padding: 0 1;
        border: round $accent;
    }
    DashboardScreen #dashboard-batch-panel.-visible {
        display: block;
    }
    DashboardScreen #dashboard-batch-meter {
        height: auto;
    }
    DashboardScreen #dashboard-batch-hint {
        height: auto;
        color: $text-muted;
    }
    DashboardScreen #dashboard-activity {
        height: 1fr;
    }
    DashboardScreen #dashboard-status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    """

    DEFAULT_MODEL_FALLBACK = "gpt-5-mini"

    def __init__(
        self,
        project: Project,
        *,
        provider_factory: ProviderFactory = build_provider,
        default_model: str | None = None,
    ) -> None:
        super().__init__()
        self._project = project
        self._provider_factory = provider_factory
        self._default_model = default_model or self.DEFAULT_MODEL_FALLBACK
        self._stats: ProjectStats | None = None
        self._batch_running = False
        self._intake_running = False
        self._export_running = False

    @property
    def stats(self) -> ProjectStats | None:
        return self._stats

    @property
    def batch_running(self) -> bool:
        return self._batch_running

    @property
    def intake_running(self) -> bool:
        return self._intake_running

    @property
    def export_running(self) -> bool:
        return self._export_running

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="dashboard-body"):
            yield Static("(loading…)", id="dashboard-project", classes="panel")
            yield Static("Progress: …", id="dashboard-progress", classes="panel")
            with Vertical(id="dashboard-cost-panel", classes="panel"):
                yield Label("Cost", classes="panel-title")
                yield CostMeter(id="dashboard-cost")
            with Vertical(id="dashboard-batch-panel"):
                yield BatchProgressMeter(id="dashboard-batch-meter")
                yield Static(
                    "Tip: press [b]o[/b] to open the [b]Reader[/b] and watch "
                    "segments translate live.",
                    id="dashboard-batch-hint",
                    markup=True,
                )
            yield Static(
                "Inbox: …",
                id="dashboard-inbox-digest",
                classes="panel",
            )
            yield Static(
                "(no activity yet)",
                id="dashboard-activity",
                classes="panel",
                markup=True,
            )
            yield Static("Ready.", id="dashboard-status", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        self._refresh_stats()

    # ------------- state helpers -------------

    def _refresh_stats(self) -> None:
        stats = compute_stats(self._project.engine, self._project.project_id)
        alerts = recent_alerts(
            self._project.engine, project_id=self._project.project_id, limit=5
        )
        self._stats = stats
        proj_widget = self.query_one("#dashboard-project", Static)
        proj_widget.update(
            f"[b]{self._project.name}[/b]  "
            f"({self._project.source_lang} → {self._project.target_lang})\n"
            f"  source: {self._project.original_epub_path}"
        )

        progress = self.query_one("#dashboard-progress", Static)
        translated = stats.translated_count
        approved = stats.approved_count
        flagged = stats.flagged_count
        pending = stats.segment_count - translated
        progress.update(
            "[b]Progress[/b]: "
            f"{translated}/{stats.segment_count} translated "
            f"({stats.progress_ratio * 100:5.1f}%) — "
            f"approved {approved}, flagged {flagged}, pending {pending}"
        )

        meter = self.query_one("#dashboard-cost", CostMeter)
        meter.update_values(spend_usd=stats.spend_usd, budget_usd=stats.budget_usd)

        digest = self.query_one("#dashboard-inbox-digest", Static)
        proposed = self._count_proposed_entries()
        alerts_count = len(alerts)
        digest.update(
            f"[b]Inbox[/b]: flagged segments = {flagged}, "
            f"proposed entries = {proposed}, "
            f"alerts = {alerts_count}"
        )

        activity = self.query_one("#dashboard-activity", Static)
        activity.update(self._format_activity(alerts))

    def _format_activity(self, alerts: list[repo.EventRow]) -> str:
        if not alerts:
            return "[b]Recent activity[/b]\n  (no events yet)"
        lines = ["[b]Recent activity[/b]"]
        for ev in alerts:
            summary = self._summarize_event(ev)
            lines.append(f"  - {ev.kind}: {summary}")
        return "\n".join(lines)

    @staticmethod
    def _summarize_event(ev: repo.EventRow) -> str:
        if ev.kind == "batch.completed":
            payload = ev.payload
            return (
                f"translated={payload.get('translated', 0)}, "
                f"cached={payload.get('cached', 0)}, "
                f"flagged={payload.get('flagged', 0)}, "
                f"cost=${float(payload.get('cost_usd', 0.0)):.4f}"
            )
        if ev.kind == "batch.paused":
            return str(ev.payload.get("reason", "budget cap reached"))
        if ev.kind == "segment.translation_flagged":
            return f"segment {ev.payload.get('segment_id', '?')} flagged"
        if ev.kind == "entity.proposed":
            return f"new proposed entry: {ev.payload.get('source_term', '?')}"
        if ev.kind == "project.budget_changed":
            new_b = ev.payload.get("new_budget_usd")
            return f"budget set to {new_b}" if new_b is not None else "budget cleared"
        if ev.kind == "batch.started":
            return f"started ({ev.payload.get('segment_count', '?')} segments)"
        if ev.kind == "batch.segment_failed":
            return f"segment failed: {ev.payload.get('error', '?')[:60]}"
        if ev.kind == "glossary.cascaded":
            return (
                f"cascaded {ev.payload.get('affected_count', 0)} segments for "
                f"{ev.payload.get('source_term', '?')!r}"
            )
        if ev.kind == "intake.completed":
            payload = ev.payload
            return (
                f"chunks={payload.get('chunks', 0)} "
                f"proposed={payload.get('proposed_count', 0)} "
                f"cost=${float(payload.get('cost_usd', 0.0)):.4f}"
            )
        if ev.kind == "intake.started":
            return f"started ({ev.payload.get('segment_count', '?')} segments)"
        if ev.kind == "batch.pre_pass_completed":
            payload = ev.payload
            return (
                f"chunks={payload.get('chunks', 0)} "
                f"proposed={payload.get('proposed_count', 0)} "
                f"cost=${float(payload.get('cost_usd', 0.0)):.4f}"
            )
        if ev.kind == "batch.pre_pass_started":
            return f"started ({ev.payload.get('chunk_count', '?')} chunks)"
        if ev.kind == "entity.extract_failed":
            return f"extractor failed (model={ev.payload.get('model', '?')})"
        if ev.kind == "entity.extracted":
            payload = ev.payload
            return (
                f"entities={payload.get('entities', 0)} "
                f"proposed={payload.get('proposed', 0)}"
            )
        return ""

    def _count_proposed_entries(self) -> int:
        entries = repo.list_glossary_entries(
            self._project.engine,
            self._project.project_id,
            status="proposed",
        )
        return len(entries)

    def _set_status(self, message: str) -> None:
        self.query_one("#dashboard-status", Static).update(message)

    # ------------- actions -------------

    def action_refresh(self) -> None:
        self._refresh_stats()
        self._set_status("Refreshed.")

    def action_open_reader(self) -> None:
        from epublate.app.screens.reader import ReaderScreen

        self.app.push_screen(
            ReaderScreen(
                self._project,
                provider_factory=self._provider_factory,
                model=self._default_model,
            ),
            self._on_child_screen_closed,
        )

    def action_open_glossary(self) -> None:
        from epublate.app.screens.glossary import GlossaryScreen

        self.app.push_screen(
            GlossaryScreen(self._project), self._on_child_screen_closed
        )

    def action_open_inbox(self) -> None:
        from epublate.app.screens.inbox import InboxScreen

        self.app.push_screen(
            InboxScreen(
                self._project,
                provider_factory=self._provider_factory,
                default_model=self._default_model,
            ),
            self._on_child_screen_closed,
        )

    def action_open_settings(self) -> None:
        from epublate.app.screens.settings import SettingsScreen

        self.app.push_screen(
            SettingsScreen(self._project, default_model=self._default_model),
            self._on_child_screen_closed,
        )

    def _on_child_screen_closed(self, _result: object) -> None:
        self._refresh_stats()

    def action_set_budget(self) -> None:
        current = self._stats.budget_usd if self._stats else None
        modal = BudgetModal(current_budget=current)
        self.app.push_screen(modal, self._on_budget_chosen)

    def _on_budget_chosen(self, result: BudgetRequest | None) -> None:
        if result is None:
            self._set_status("Budget unchanged.")
            return
        repo.update_project_budget(
            self._project.engine,
            project_id=self._project.project_id,
            budget_usd=result.budget_usd,
        )
        self._refresh_stats()
        if result.budget_usd is None:
            self._set_status("Budget cap cleared.")
        else:
            self._set_status(f"Budget cap set to ${result.budget_usd:.4f}.")

    def action_batch(self) -> None:
        if self._batch_running:
            self._set_status("A batch is already running.")
            return
        try:
            shapes = compute_chapter_shapes(
                self._project.engine, project_id=self._project.project_id
            )
            shape_summary = summarize_chapter_shapes(shapes)
        except Exception:
            shape_summary = None
        modal = BatchModal(
            default_model=self._default_model,
            default_concurrency=1,
            default_budget=self._stats.budget_usd if self._stats else None,
            chapter_shape_summary=shape_summary,
            project_stats=self._stats,
        )
        self.app.push_screen(modal, self._on_batch_chosen)

    def _on_batch_chosen(self, result: BatchRequest | None) -> None:
        if result is None:
            self._set_status("Batch cancelled.")
            return
        chapter_ids, ok = self._resolve_chapter_range(result.chapters)
        if not ok:
            self._set_status(
                f"Could not parse chapter range {result.chapters!r}; use '*' or '1-3'."
            )
            return
        self._batch_running = True
        self._set_status(
            f"Batch dispatched: chapters={result.chapters}, "
            f"concurrency={result.concurrency}, model={result.model}"
        )
        # Compute the upfront segment total so the progress meter knows
        # what 100% means; cheaper to count once here than to track it
        # per worker thread.
        total_pending = self._count_pending_segments(chapter_ids)
        self._show_batch_panel(total=total_pending)
        self._publish_batch_progress(
            active=True, summary=None, total=total_pending, paused=False
        )
        options = BatchOptions(
            model=result.model,
            concurrency=result.concurrency,
            budget_usd=result.budget_usd,
            chapter_ids=chapter_ids,
            bypass_cache=result.bypass_cache,
            group_small_segments=result.group_small_segments,
            group_max_items=result.group_max_items,
        )
        self._batch_worker(options=options)

    def _count_pending_segments(self, chapter_ids: tuple[str, ...] | None) -> int:
        rows = repo.list_segments_by_status(
            self._project.engine,
            project_id=self._project.project_id,
            status="pending",
            chapter_ids=chapter_ids,
        )
        return len(rows)

    def _show_batch_panel(self, *, total: int) -> None:
        panel = self.query_one("#dashboard-batch-panel", Vertical)
        panel.add_class("-visible")
        meter = self.query_one("#dashboard-batch-meter", BatchProgressMeter)
        meter.update_snapshot(BatchSnapshot(attempted=0, total=total))

    def _hide_batch_panel(self) -> None:
        panel = self.query_one("#dashboard-batch-panel", Vertical)
        panel.remove_class("-visible")
        meter = self.query_one("#dashboard-batch-meter", BatchProgressMeter)
        meter.update_snapshot(None)

    def _publish_batch_progress(
        self,
        *,
        active: bool,
        summary: BatchSummary | None,
        total: int,
        paused: bool,
    ) -> None:
        """Mirror the batch state on the app so other screens can read it.

        Other screens (Reader) treat the slot as advisory: they'll only
        consult it on their refresh tick. Mutating an attribute is fine
        because Textual's main loop is single-threaded.
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

    def _resolve_chapter_range(self, expr: str) -> tuple[tuple[str, ...] | None, bool]:
        """Translate ``*`` / ``N`` / ``A-B`` into ``(chapter_ids, ok)``.

        ``chapter_ids`` is ``None`` for "all chapters" (which is what
        :class:`BatchOptions` expects) or a non-empty tuple for an
        explicit range. ``ok`` is ``False`` only on parse failure;
        callers must check it before dispatching the batch.
        """

        chapters = repo.list_chapters(self._project.engine, self._project.project_id)
        expr = expr.strip()
        if expr in ("", "*"):
            return None, True
        if not chapters:
            return None, False
        try:
            if "-" in expr:
                lo_s, hi_s = expr.split("-", 1)
                lo = int(lo_s)
                hi = int(hi_s)
            else:
                lo = hi = int(expr)
        except ValueError:
            return None, False
        if lo < 1 or hi < lo or hi > len(chapters):
            return None, False
        return tuple(chapters[i - 1].id for i in range(lo, hi + 1)), True

    @work(exclusive=True, group="batch", thread=True)
    def _batch_worker(self, *, options: BatchOptions) -> None:
        provider = self._provider_factory()
        try:
            summary = run_batch(
                engine=self._project.engine,
                project_id=self._project.project_id,
                source_lang=self._project.source_lang,
                target_lang=self._project.target_lang,
                provider=provider,
                options=options,
                on_progress=self._post_progress,
            )
        except BatchPaused as paused:
            self.post_message(BatchFinished(paused.summary, paused=True))
            return
        except Exception as exc:
            self.post_message(BatchFailed(str(exc)))
            return
        self.post_message(BatchFinished(summary, paused=False))

    def _post_progress(self, event: BatchProgressEvent) -> None:
        # Called from the worker thread; ``post_message`` is thread-safe.
        self.post_message(BatchTick(event))

    @on(BatchTick)
    def _handle_tick(self, message: BatchTick) -> None:
        ev = message.event
        meter = self.query_one("#dashboard-cost", CostMeter)
        meter.spend_usd = ev.summary.cost_usd
        progress_meter = self.query_one("#dashboard-batch-meter", BatchProgressMeter)
        # Total is the upfront pending count; if we somehow attempted
        # more than that (e.g. the user retried) widen the bar so it
        # never visually exceeds 100%.
        from epublate.app.main import EpublateApp

        app = self.app
        total = (
            int(getattr(app.batch_progress, "total", 0))
            if isinstance(app, EpublateApp)
            else 0
        )
        total = max(total, ev.summary.attempted)
        progress_meter.update_snapshot(
            BatchSnapshot(
                attempted=ev.summary.attempted,
                total=total,
                translated=ev.summary.translated,
                cached=ev.summary.cached,
                flagged=ev.summary.flagged,
                failed=ev.summary.failed,
                cost_usd=ev.summary.cost_usd,
                elapsed_s=ev.summary.elapsed_s,
                paused=False,
            )
        )
        self._publish_batch_progress(
            active=True,
            summary=ev.summary,
            total=total,
            paused=False,
        )
        self._set_status(
            f"Batch: {ev.summary.attempted}/{total} done — translated="
            f"{ev.summary.translated}, cached={ev.summary.cached}, "
            f"flagged={ev.summary.flagged}, failed={ev.summary.failed}, "
            f"cost=${ev.summary.cost_usd:.4f}"
        )

    @on(BatchFinished)
    def _handle_batch_finished(self, message: BatchFinished) -> None:
        self._batch_running = False
        s = message.summary
        kind = "Batch paused" if message.paused else "Batch complete"
        reason = f" ({s.paused_reason})" if message.paused and s.paused_reason else ""
        progress_meter = self.query_one("#dashboard-batch-meter", BatchProgressMeter)
        from epublate.app.main import EpublateApp

        app = self.app
        total = (
            int(getattr(app.batch_progress, "total", 0))
            if isinstance(app, EpublateApp)
            else max(s.attempted, 1)
        )
        total = max(total, s.attempted)
        progress_meter.update_snapshot(
            BatchSnapshot(
                attempted=s.attempted,
                total=total,
                translated=s.translated,
                cached=s.cached,
                flagged=s.flagged,
                failed=s.failed,
                cost_usd=s.cost_usd,
                elapsed_s=s.elapsed_s,
                paused=message.paused,
                paused_reason=s.paused_reason,
            )
        )
        self._publish_batch_progress(
            active=False,
            summary=s,
            total=total,
            paused=message.paused,
        )
        self._refresh_stats()
        self._set_status(
            f"{kind}: translated={s.translated}, cached={s.cached}, "
            f"flagged={s.flagged}, failed={s.failed}, "
            f"cost=${s.cost_usd:.4f}, elapsed={s.elapsed_s:.2f}s{reason}"
        )
        # Leave the panel visible after completion so the curator can
        # review the final tally; it is replaced on the next batch.

    @on(BatchFailed)
    def _handle_batch_failed(self, message: BatchFailed) -> None:
        self._batch_running = False
        self._publish_batch_progress(active=False, summary=None, total=0, paused=False)
        self._hide_batch_panel()
        self._set_status(f"Batch failed: {message.error}")

    # ------------- intake (M5) -------------

    def action_intake(self) -> None:
        if self._intake_running:
            self._set_status("An intake pass is already running.")
            return
        try:
            default_helper = resolve_helper_model(self._default_model)
        except Exception:
            default_helper = self._default_model
        modal = IntakeModal(
            default_helper_model=default_helper,
            default_max_segments=DEFAULT_INTAKE_MAX_SEGMENTS,
        )
        self.app.push_screen(modal, self._on_intake_chosen)

    def _on_intake_chosen(self, result: IntakeRequest | None) -> None:
        if result is None:
            self._set_status("Intake cancelled.")
            return
        self._intake_running = True
        self._set_status(
            f"Intake dispatched: helper={result.helper_model}, "
            f"max_segments={result.max_segments}"
        )
        options = IntakeOptions(
            model=result.helper_model,
            max_segments=result.max_segments,
        )
        self._intake_worker(options=options)

    @work(exclusive=True, group="intake", thread=True)
    def _intake_worker(self, *, options: IntakeOptions) -> None:
        provider = self._provider_factory()
        try:
            summary = run_book_intake(
                engine=self._project.engine,
                project_id=self._project.project_id,
                source_lang=self._project.source_lang,
                target_lang=self._project.target_lang,
                provider=provider,
                options=options,
            )
        except Exception as exc:
            self.post_message(IntakeFailed(str(exc)))
            return
        self.post_message(IntakeFinished(summary))

    @on(IntakeFinished)
    def _handle_intake_finished(self, message: IntakeFinished) -> None:
        self._intake_running = False
        self._refresh_stats()
        s = message.summary
        pov = f", pov={s.pov}" if s.pov else ""
        tense = f", tense={s.tense}" if s.tense else ""
        self._set_status(
            f"Intake complete: {s.chunks} chunks ({s.cached_chunks} cached), "
            f"{s.proposed_count} proposed, ${s.cost_usd:.4f}{pov}{tense}"
        )

    @on(IntakeFailed)
    def _handle_intake_failed(self, message: IntakeFailed) -> None:
        self._intake_running = False
        self._set_status(f"Intake failed: {message.error}")

    # ------------- export (PRD §7.6 / F-IO-7) -------------

    def action_export(self) -> None:
        if self._export_running:
            self._set_status("An export is already in flight.")
            return
        modal = ExportModal(
            default_path=default_export_path(self._project),
            project_stats=self._stats,
        )
        self.app.push_screen(modal, self._on_export_chosen)

    def _on_export_chosen(self, result: ExportRequest | None) -> None:
        if result is None:
            self._set_status("Export cancelled.")
            return
        self._export_running = True
        self._set_status(
            f"Export dispatched: out={result.out_path}, "
            f"epubcheck={'yes' if result.epubcheck else 'no'}"
        )
        self._export_worker(
            out_path=result.out_path,
            epubcheck=result.epubcheck,
        )

    @work(exclusive=True, group="export", thread=True)
    def _export_worker(self, *, out_path: Path, epubcheck: bool) -> None:
        try:
            written = self._project.export(out_path, epubcheck=epubcheck)
        except Exception as exc:
            _logger.exception("export failed for %s", out_path)
            self.post_message(ExportFailed(str(exc)))
            return
        report = self._project.last_epubcheck_report
        summary = report.summary_line() if report is not None else None
        status = report.status if report is not None else None
        self.post_message(
            ExportFinished(
                out_path=written,
                epubcheck_summary=summary,
                epubcheck_status=status,
            )
        )

    @on(ExportFinished)
    def _handle_export_finished(self, message: ExportFinished) -> None:
        self._export_running = False
        self._refresh_stats()
        suffix = ""
        if message.epubcheck_summary is not None:
            suffix = f" — epubcheck: {message.epubcheck_summary}"
        self._set_status(f"Saved {message.out_path}{suffix}")

    @on(ExportFailed)
    def _handle_export_failed(self, message: ExportFailed) -> None:
        self._export_running = False
        self._set_status(f"Export failed: {message.error}")


__all__ = [
    "BatchFailed",
    "BatchFinished",
    "BatchModal",
    "BatchRequest",
    "BatchTick",
    "BudgetModal",
    "BudgetRequest",
    "DashboardScreen",
    "ExportFailed",
    "ExportFinished",
    "ExportModal",
    "ExportRequest",
    "IntakeFailed",
    "IntakeFinished",
    "IntakeModal",
    "IntakeRequest",
    "ProviderFactory",
    "default_export_path",
]
