"""Projects screen — first stop in the TUI (PRD §4.6).

This is the landing screen for ``epublate`` (no subcommand). It lists
recently-opened projects, lets the curator create or open projects
in-TUI without dropping back to the CLI, and pushes the Dashboard for
the chosen project.

The recents store lives at ``~/.config/epublate/recents.json`` (see
:mod:`epublate.app.recents`). It's purely a UI convenience — the
authoritative project state still lives in each project's SQLite DB.
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Center, Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from epublate.app.branding import (
    EPUBLATE_LOGO,
    ICON_ACTIVE,
    ICON_BULLET,
    ICON_DONE,
    ICON_MISSING,
    TAGLINE,
    progress_bar,
)
from epublate.app.config import UIConfig, resolve_auto_tone_sniff
from epublate.app.paths import default_projects_root
from epublate.app.recents import RecentProject, RecentsStore
from epublate.app.screens.new_project import NewProjectModal, ProviderFactory
from epublate.app.screens.open_project import OpenProjectModal
from epublate.core.stats import compute_stats

if TYPE_CHECKING:
    from epublate.core.project import Project
    from epublate.llm.base import LLMProvider

_logger = logging.getLogger(__name__)


_HERO_HINT = (
    f"  {ICON_BULLET} [b]n[/b] new project   "
    f"{ICON_BULLET} [b]o[/b] open by path   "
    f"{ICON_BULLET} [b]enter[/b] open selected   "
    f"{ICON_BULLET} [b]?[/b] help"
)

_EMPTY_BODY = (
    "[b]Welcome to epublate.[/b]\n\n"
    "No projects opened yet — let's fix that.\n\n"
    "  [b yellow]n[/]  Create a new project from a [i]source.epub[/]\n"
    "  [b yellow]o[/]  Open an existing project folder by path\n"
    "  [b yellow]?[/]  Cheat sheet (or [b]F1[/])  ·  "
    "[b yellow]T[/]  Cycle theme  ·  [b yellow]q[/]  Quit"
)


class ProjectsScreen(Screen[None]):
    """Landing screen with recents + in-TUI new/open flows."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("n", "new_project", "New", show=True),
        Binding("o", "open_project", "Open", show=True),
        Binding("delete", "remove_selected", "Remove", show=True),
        Binding("x", "remove_selected", "Remove", show=False),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("q", "app.quit", "Quit", show=True),
        Binding("d", "app.toggle_dark", "Toggle dark", show=False),
    ]

    DEFAULT_CSS = """
    ProjectsScreen {
        layers: base overlay;
        background: $background;
    }
    ProjectsScreen #projects-hero {
        height: auto;
        padding: 1 2 0 2;
        background: $background;
    }
    ProjectsScreen #projects-logo {
        color: $primary;
        text-style: bold;
        content-align: center middle;
        height: auto;
        padding: 0;
    }
    ProjectsScreen #projects-tagline {
        color: $text-muted;
        content-align: center middle;
        height: 1;
        padding: 0 0 1 0;
    }
    ProjectsScreen #projects-hero-hint {
        color: $text;
        content-align: center middle;
        height: 1;
        padding: 0 0 1 0;
    }
    ProjectsScreen #projects-summary {
        height: auto;
        padding: 1 2;
        margin: 0 2 0 2;
        border: round $accent;
        color: $text;
        background: $panel;
    }
    ProjectsScreen #projects-section-header {
        height: 1;
        padding: 0 2;
        margin: 1 0 0 0;
        color: $text-muted;
        text-style: bold;
    }
    ProjectsScreen #projects-table-wrap {
        height: 1fr;
        padding: 0 2;
    }
    ProjectsScreen #projects-table {
        height: 1fr;
        border: round $primary;
        background: $surface;
    }
    ProjectsScreen DataTable > .datatable--header {
        text-style: bold;
        background: $panel;
        color: $primary;
    }
    ProjectsScreen DataTable > .datatable--cursor {
        background: $accent 35%;
        color: $text;
        text-style: bold;
    }
    ProjectsScreen DataTable > .datatable--hover {
        background: $boost;
    }
    ProjectsScreen #projects-empty-wrap {
        height: 1fr;
        padding: 0 2;
        align: center middle;
    }
    ProjectsScreen #projects-empty {
        height: auto;
        width: 70;
        padding: 2 4;
        border: round $primary;
        background: $panel;
        content-align: center middle;
    }
    ProjectsScreen #projects-status {
        dock: bottom;
        height: 1;
        padding: 0 2;
        background: $boost;
        color: $text-muted;
    }
    """

    COLUMN_KEYS = (" ", "Name", "Languages", "Progress", "Updated", "Path")

    def __init__(
        self,
        *,
        recents_path: Path | None = None,
        provider_factory: object | None = None,
        default_model: str | None = None,
        ui_config: UIConfig | None = None,
    ) -> None:
        super().__init__()
        # ``recents_path`` is exposed for the test suite so we don't poke
        # the real ``~/.config/epublate``. Production code uses the
        # default which resolves XDG-style.
        self._recents_path = recents_path
        self._provider_factory = provider_factory
        self._default_model = default_model
        # Snapshot the UIConfig at construction time. We never mutate
        # the in-memory copy here — the Settings screen owns persistence
        # of any preference flips. Tests can pass a synthetic config to
        # exercise specific toggle states (auto tone-sniff, theme, …).
        self._ui_config: UIConfig = ui_config or UIConfig()
        self._store: RecentsStore = RecentsStore()
        self._active_project: Project | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            with Vertical(id="projects-hero"):
                yield Static(EPUBLATE_LOGO, id="projects-logo", markup=False)
                yield Static(TAGLINE, id="projects-tagline", markup=False)
                yield Static(_HERO_HINT, id="projects-hero-hint", markup=True)
            yield Static(
                self._summary_line(),
                id="projects-summary",
                markup=True,
            )
            yield Static(
                f"  {ICON_BULLET} Recent projects",
                id="projects-section-header",
                markup=True,
            )
            with Vertical(id="projects-table-wrap"):
                table: DataTable[str] = DataTable(
                    id="projects-table",
                    zebra_stripes=True,
                    cursor_type="row",
                    show_header=True,
                )
                table.add_columns(*self.COLUMN_KEYS)
                yield table
            with Center(id="projects-empty-wrap"):
                yield Static(_EMPTY_BODY, id="projects-empty", markup=True)
            yield Static(
                self._idle_status(),
                id="projects-status",
                markup=True,
            )
        yield Footer()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self._reload_store()
        # Quietly drop recents entries whose folders have vanished (test
        # scratch dirs, sshfs mounts gone, etc.) so the landing screen
        # isn't a graveyard of dead rows on first boot.
        pruned = self._store.prune_missing()
        if pruned:
            self._save_store_safely()
        self._refresh_table(initial=True)

    def on_unmount(self) -> None:
        self._close_active_project()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        self._reload_store()
        pruned = self._store.prune_missing()
        if pruned:
            self._save_store_safely()
            self._set_status(
                f"Pruned {len(pruned)} project(s) whose folders no longer exist."
            )
        self._refresh_table()
        if not pruned:
            self._set_status("Reloaded recents.")

    def action_new_project(self) -> None:
        sniff_factory, helper_model = self._build_tone_sniff_wiring()
        modal = NewProjectModal(
            recents_path=self._recents_path,
            auto_tone_sniff=resolve_auto_tone_sniff(self._ui_config),
            tone_provider_factory=sniff_factory,
            tone_helper_model=helper_model,
        )
        self.app.push_screen(modal, self._on_new_project_done)

    def _build_tone_sniff_wiring(
        self,
    ) -> tuple[ProviderFactory | None, str | None]:
        """Resolve the (provider_factory, helper_model) pair for the modal.

        The factory is wrapped so we never attempt to build the helper
        provider until the curator actually picks an .epub — this keeps
        the modal cheap to open and lets it survive in environments
        where ``EPUBLATE_LLM_MODEL`` simply isn't set. ``helper_model``
        is resolved up-front (it doesn't make a network call) via
        :func:`epublate.llm.factory.resolve_helper_model`.

        Both halves return ``None`` on missing config so the modal
        gracefully skips the sniff instead of erroring out.
        """

        from epublate.llm.factory import build_provider, resolve_helper_model

        try:
            helper_model = resolve_helper_model(self._default_model)
        except Exception as exc:  # ConfigurationError + paranoia
            _logger.debug("tone sniff disabled: %s", exc)
            return None, None

        if self._provider_factory is not None:
            factory: ProviderFactory = self._provider_factory  # type: ignore[assignment]
        else:

            def factory() -> LLMProvider:
                return build_provider()

        return factory, helper_model

    def action_open_project(self) -> None:
        modal = OpenProjectModal(recents_path=self._recents_path)
        self.app.push_screen(modal, self._on_open_project_done)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # Pressing Enter (or double-click) on a recents row picks that
        # project. We resolve the entry via the cursor row index since
        # Textual's row_key isn't always preserved across reloads.
        del event
        entry = self._selected_entry()
        if entry is not None:
            self._open_recent(entry)

    def action_remove_selected(self) -> None:
        entry = self._selected_entry()
        if entry is None:
            self._set_status("Nothing selected to remove.")
            return
        if self._store.remove(entry.project_dir):
            self._save_store_safely()
            self._refresh_table()
            self._set_status(
                f"Removed {entry.name!r} from recents (project files left untouched)."
            )

    # ------------------------------------------------------------------
    # Modal callbacks
    # ------------------------------------------------------------------

    def _on_new_project_done(self, project: Project | None) -> None:
        if project is None:
            self._set_status("New project canceled.")
            return
        self._reload_store()
        self._refresh_table()
        self._open_project_handle(project)

    def _on_open_project_done(self, project: Project | None) -> None:
        if project is None:
            self._set_status("Open canceled.")
            return
        self._reload_store()
        self._refresh_table()
        self._open_project_handle(project)

    # ------------------------------------------------------------------
    # Project lifecycle
    # ------------------------------------------------------------------

    def _open_recent(self, entry: RecentProject) -> None:
        if not entry.exists():
            self._set_status(
                f"{entry.name!r}: folder no longer exists. Press R to prune."
            )
            return
        from epublate.app.recents import record_project
        from epublate.core.project import Project as _Project

        try:
            project = _Project.open(entry.path)
        except Exception as exc:
            _logger.exception("could not open recent project %s", entry.project_dir)
            self._set_status(f"Could not open {entry.name!r}: {exc}")
            return
        record_project(
            project_dir=project.project_dir,
            name=project.name,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            path=self._recents_path,
        )
        self._reload_store()
        self._refresh_table()
        self._open_project_handle(project)

    def _open_project_handle(self, project: Project) -> None:
        from epublate.app.screens.dashboard import DashboardScreen

        self._close_active_project()
        self._active_project = project

        provider_factory = self._provider_factory
        if provider_factory is None:
            from epublate.llm.factory import build_provider

            provider_factory = build_provider

        from epublate.app.screens.reader import DEFAULT_MODEL

        screen = DashboardScreen(
            project,
            provider_factory=provider_factory,  # type: ignore[arg-type]
            default_model=self._default_model or DEFAULT_MODEL,
        )
        self.app.push_screen(screen, self._on_dashboard_closed)
        self._set_status(f"Opened {project.name!r}.")

    def _on_dashboard_closed(self, _result: object) -> None:
        self._close_active_project()
        self._reload_store()
        self._refresh_table()
        self._set_status("Back to projects.")

    def _close_active_project(self) -> None:
        if self._active_project is None:
            return
        try:
            self._active_project.close()
        except Exception:
            _logger.exception("error closing project")
        finally:
            self._active_project = None

    # ------------------------------------------------------------------
    # Store + table helpers
    # ------------------------------------------------------------------

    def _reload_store(self) -> None:
        self._store = RecentsStore.load(self._recents_path)

    def _save_store_safely(self) -> None:
        try:
            self._store.save(self._recents_path)
        except OSError as exc:
            _logger.warning("could not persist recents: %s", exc)

    def _refresh_table(self, *, initial: bool = False) -> None:
        del initial
        table = self.query_one("#projects-table", DataTable)
        table.clear()
        for entry in self._store.entries:
            updated = _format_timestamp(entry.last_opened)
            ratio, progress_text = self._compute_progress(entry)
            icon = self._row_icon(entry, ratio)
            table.add_row(
                icon,
                f"[b]{_escape(entry.name)}[/]",
                f"[cyan]{entry.source_lang}[/] → [yellow]{entry.target_lang}[/]",
                progress_text,
                f"[dim]{_escape(updated)}[/]",
                f"[dim]{_escape(_shorten_path(entry.project_dir))}[/]",
                key=entry.project_dir,
            )

        has_entries = bool(self._store.entries)
        # Toggle the empty-state card vs the table.
        empty_wrap = self.query_one("#projects-empty-wrap")
        section = self.query_one("#projects-section-header")
        table_wrap = self.query_one("#projects-table-wrap")
        empty_wrap.display = not has_entries
        section.display = has_entries
        table_wrap.display = has_entries

        # Always refresh the summary line — the count changes as the
        # store evolves.
        self.query_one("#projects-summary", Static).update(self._summary_line())

    def _selected_entry(self) -> RecentProject | None:
        table = self.query_one("#projects-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_index = int(table.cursor_row)
        except (AttributeError, TypeError, ValueError):
            return None
        if row_index < 0 or row_index >= len(self._store.entries):
            return None
        # The table mirrors ``self._store.entries`` order, so the row
        # cursor is a direct index into the in-memory list.
        return self._store.entries[row_index]

    def _compute_progress(self, entry: RecentProject) -> tuple[float, str]:
        if not entry.exists():
            return 0.0, f"[red]{ICON_MISSING} missing[/]"
        # Best-effort read; never let a stale or locked DB break the
        # listing — that just degrades to "(?)".
        from epublate.core.project import Project as _Project

        try:
            project = _Project.open(entry.path)
        except Exception:
            return 0.0, "[dim](?)[/]"
        try:
            stats = compute_stats(project.engine, project.project_id)
            ratio = stats.progress_ratio
        except Exception:
            return 0.0, "[dim](?)[/]"
        finally:
            project.close()

        bar = progress_bar(ratio, width=12)
        color = _rich_progress_color(ratio)
        pct = f"{ratio * 100:5.1f}%"
        return ratio, f"[{color}]{bar}[/] [b]{pct}[/]"

    def _row_icon(self, entry: RecentProject, ratio: float) -> str:
        if not entry.exists():
            return f"[red]{ICON_MISSING}[/]"
        if ratio >= 0.999:
            return f"[green]{ICON_DONE}[/]"
        return f"[yellow]{ICON_ACTIVE}[/]"

    def _summary_line(self) -> str:
        n = len(self._store.entries)
        try:
            root = str(default_projects_root())
        except Exception:
            root = "~/Documents/epublate"
        home = str(Path.home())
        if root.startswith(home):
            root = "~" + root[len(home) :]
        root_display = _escape(root)
        if n == 0:
            return (
                "  [dim]No recent projects yet — press [b]n[/] "
                "to create your first one.[/]\n"
                f"  [dim]New projects land in:[/] [b]{root_display}[/]"
            )
        alive = sum(1 for e in self._store.entries if e.exists())
        if alive == n:
            tail = f"[green]{alive} ready[/]"
        else:
            tail = f"[green]{alive} ready[/]  [red]{n - alive} missing[/]"
        return (
            f"  [yellow][b]{n}[/][/] recent project{'s' if n != 1 else ''}"
            f"   [dim]·[/]   {tail}"
            f"   [dim]·[/]   "
            f"[dim]new projects land in[/] [b]{root_display}[/]"
        )

    def _idle_status(self) -> str:
        return (
            "[dim] Ready  ·  press [/]"
            "[b]?[/]"
            "[dim] for help, [/]"
            "[b]T[/]"
            "[dim] to switch theme[/]"
        )

    def _set_status(self, message: str) -> None:
        self.query_one("#projects-status", Static).update(message)


def _format_timestamp(ts: float) -> str:
    if not ts:
        return "—"
    delta = time.time() - ts
    if delta < 0:
        delta = 0.0
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86_400:
        return f"{int(delta // 3600)}h ago"
    if delta < 604_800:
        return f"{int(delta // 86_400)}d ago"
    return datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")


def _rich_progress_color(ratio: float) -> str:
    """Pick a Rich-named color tag for a progress ratio.

    DataTable cells go through Rich markup (not Textual's CSS-aware
    content markup), so we stick to safe Rich color names that
    render identically across themes.
    """

    if ratio >= 0.99:
        return "green"
    if ratio >= 0.66:
        return "cyan"
    if ratio >= 0.33:
        return "yellow"
    return "dim"


def _shorten_path(path: str, max_len: int = 50) -> str:
    """Replace ``$HOME`` with ``~`` and ellipsize long absolute paths."""

    home = str(Path.home())
    if path.startswith(home):
        path = "~" + path[len(home) :]
    if len(path) <= max_len:
        return path
    keep = max_len - 1
    head = keep // 2
    tail = keep - head
    return path[:head] + "…" + path[-tail:]


def _escape(text: str) -> str:
    """Escape Rich markup in user-controlled strings."""

    return text.replace("[", r"\[")


__all__ = ["ProjectsScreen"]
