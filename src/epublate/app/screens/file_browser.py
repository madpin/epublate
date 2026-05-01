"""Interactive file browser modal (PRD §4.6).

The New / Open project modals used to ask curators for an absolute
path typed into a single Input — fine for scripts, miserable for
anyone who just downloaded a book. This modal is the friendlier
alternative:

* **Quick locations bar** at the top with one-press shortcuts for
  Current folder, Home, Documents, Downloads, Desktop, and the
  projects root (if configured).
* **Navigable list** underneath: ``..`` takes you up, ``Enter`` on a
  directory descends into it, ``Enter`` on a file picks it.
* **Filter** toggles between "show ePub + directories" (source
  picker) and "directories only" (project-dir picker). Hidden
  entries are suppressed by default and can be toggled with ``.``.
* **Mouse compatible**: every row is clickable; double-click
  navigates or picks depending on the kind.

The modal returns either the picked :class:`~pathlib.Path` or
``None`` when canceled. Callers can seed the starting directory
via ``start_path``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, Literal

from rich.markup import escape as escape_markup
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Input, Label, Static

from epublate.app.paths import QuickLocation, quick_locations

_logger = logging.getLogger(__name__)


BrowserMode = Literal["epub", "directory"]


@dataclass(slots=True, frozen=True)
class _Entry:
    """One row in the browser."""

    name: str
    path: Path
    is_dir: bool
    is_parent: bool = False
    size_bytes: int | None = None


class FileBrowserModal(ModalScreen[Path | None]):
    """Interactive directory / ePub picker.

    ``mode="epub"`` shows ``.epub`` files and directories; ``Enter``
    on an ePub returns it. ``mode="directory"`` hides files; ``Enter``
    on the current dir (``Pick this folder`` button) returns the
    current path, letting curators pick a project folder after
    drilling in.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("enter", "activate", "Open / Pick", show=True),
        Binding("backspace,left", "go_up", "Up", show=True),
        Binding("h", "go_home", "Home", show=False),
        Binding("ctrl+r", "refresh", "Refresh", show=True),
        Binding("period,full_stop", "toggle_hidden", "Hidden", show=True),
        Binding("1", "quick_location('1')", "", show=False),
        Binding("2", "quick_location('2')", "", show=False),
        Binding("3", "quick_location('3')", "", show=False),
        Binding("4", "quick_location('4')", "", show=False),
        Binding("5", "quick_location('5')", "", show=False),
        Binding("6", "quick_location('6')", "", show=False),
    ]

    DEFAULT_CSS = """
    FileBrowserModal {
        align: center middle;
        background: $background 60%;
    }
    FileBrowserModal #fb-box {
        width: 90%;
        max-width: 120;
        height: 85%;
        max-height: 90%;
        border: round $accent;
        padding: 1 2;
        background: $panel;
    }
    FileBrowserModal #fb-title {
        text-style: bold;
        color: $primary;
    }
    FileBrowserModal #fb-subtitle {
        color: $text-muted;
        height: 1;
        padding: 0 0 1 0;
    }
    FileBrowserModal #fb-quick {
        height: auto;
        padding: 0 0 1 0;
    }
    FileBrowserModal .fb-quick-btn {
        height: 1;
        min-width: 14;
        margin: 0 1 0 0;
        padding: 0 1;
        background: $surface;
        color: $text;
        border: none;
    }
    FileBrowserModal .fb-quick-btn:hover {
        background: $boost;
    }
    FileBrowserModal .fb-quick-btn.-current {
        background: $accent 30%;
        color: $accent;
        text-style: bold;
    }
    FileBrowserModal #fb-path {
        color: $text;
        padding: 0 0 1 0;
        height: 1;
    }
    FileBrowserModal #fb-path-input {
        height: 3;
        margin: 0 0 1 0;
    }
    FileBrowserModal #fb-table {
        height: 1fr;
        border: round $primary;
        background: $surface;
    }
    FileBrowserModal DataTable > .datatable--cursor {
        background: $accent 35%;
        color: $text;
        text-style: bold;
    }
    FileBrowserModal DataTable > .datatable--hover {
        background: $boost;
    }
    FileBrowserModal #fb-selected {
        color: $text-muted;
        height: 1;
        padding: 1 0 0 0;
    }
    FileBrowserModal #fb-buttons {
        height: 3;
        padding: 1 0 0 0;
        align-horizontal: right;
    }
    FileBrowserModal #fb-buttons Button {
        margin: 0 0 0 1;
    }
    """

    COLUMNS: ClassVar[tuple[str, ...]] = ("", "Name", "Size")

    def __init__(
        self,
        *,
        mode: BrowserMode = "epub",
        start_path: Path | None = None,
        title: str = "Pick a file",
        subtitle: str | None = None,
    ) -> None:
        super().__init__()
        self._mode = mode
        self._title = title
        self._subtitle = subtitle or (
            "Pick a .epub file" if mode == "epub" else "Pick a project folder"
        )
        self._show_hidden = False
        self._entries: list[_Entry] = []
        self._locations: list[QuickLocation] = quick_locations()
        start = start_path or (
            self._locations[0].path if self._locations else Path.cwd()
        )
        try:
            start = start.expanduser().resolve()
        except OSError:
            start = Path.cwd().resolve()
        if not start.is_dir():
            start = start.parent if start.parent.is_dir() else Path.cwd().resolve()
        self._current: Path = start

    # ------------------------------------------------------------------
    # Compose
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Vertical(id="fb-box"):
            yield Label(self._title, id="fb-title")
            yield Static(self._subtitle, id="fb-subtitle", markup=False)
            with Horizontal(id="fb-quick"):
                for loc in self._locations:
                    label = (
                        f"{loc.shortcut}. {loc.label}" if loc.shortcut else loc.label
                    )
                    btn = Button(
                        label,
                        id=self._quick_button_id(loc),
                        classes="fb-quick-btn",
                    )
                    yield btn
            yield Static(self._path_text(), id="fb-path", markup=True)
            yield Input(
                value=str(self._current),
                placeholder="Type a path, press Enter to jump",
                id="fb-path-input",
            )
            table: DataTable[str] = DataTable(
                id="fb-table",
                cursor_type="row",
                show_header=True,
                zebra_stripes=True,
            )
            table.add_columns(*self.COLUMNS)
            yield table
            yield Static(self._selected_text(None), id="fb-selected", markup=True)
            with Horizontal(id="fb-buttons"):
                yield Button("Cancel", id="fb-cancel", variant="default")
                if self._mode == "directory":
                    yield Button(
                        "Pick this folder",
                        id="fb-pick-here",
                        variant="primary",
                    )
                yield Button("Open / Pick", id="fb-activate", variant="primary")
        yield Footer()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self._reload_entries()
        self._sync_quick_highlight()
        table = self.query_one("#fb-table", DataTable)
        table.focus()

    # ------------------------------------------------------------------
    # Navigation
    # ------------------------------------------------------------------

    def _path_text(self) -> str:
        return f"[b]Path:[/b] [dim]{escape_markup(str(self._current))}[/dim]"

    def _selected_text(self, entry: _Entry | None) -> str:
        if entry is None or entry.is_parent:
            return "[dim]Selected: (none)[/dim]"
        return f"[b]Selected:[/b] {escape_markup(str(entry.path))}"

    @staticmethod
    def _quick_button_id(loc: QuickLocation) -> str:
        suffix = loc.label.lower().replace(" ", "-")
        return f"fb-quick-{suffix}"

    def _sync_quick_highlight(self) -> None:
        for loc in self._locations:
            try:
                button = self.query_one(f"#{self._quick_button_id(loc)}", Button)
            except Exception:
                continue
            if loc.path == self._current:
                button.add_class("-current")
            else:
                button.remove_class("-current")

    def _reload_entries(self) -> None:
        self.query_one("#fb-path", Static).update(self._path_text())
        self.query_one("#fb-path-input", Input).value = str(self._current)
        self._entries = list(self._scan_current_dir())
        table = self.query_one("#fb-table", DataTable)
        table.clear()
        for entry in self._entries:
            icon, name, size = self._format_row(entry)
            table.add_row(icon, name, size)
        if self._entries:
            table.move_cursor(row=0, column=0)
        self._update_selection_label(self._entry_at_cursor())
        self._sync_quick_highlight()

    def _scan_current_dir(self) -> Iterable[_Entry]:
        # Parent first so Backspace-style navigation is always obvious.
        if self._current.parent != self._current:
            yield _Entry(
                name="..",
                path=self._current.parent,
                is_dir=True,
                is_parent=True,
            )
        try:
            raw = list(self._current.iterdir())
        except OSError as exc:
            _logger.warning("could not list %s: %s", self._current, exc)
            return
        dirs: list[_Entry] = []
        files: list[_Entry] = []
        for path in raw:
            name = path.name
            if not self._show_hidden and name.startswith("."):
                continue
            try:
                is_dir = path.is_dir()
            except OSError:
                continue
            if is_dir:
                dirs.append(_Entry(name=name, path=path, is_dir=True))
                continue
            if self._mode == "directory":
                continue
            if not self._passes_filter(path):
                continue
            size = self._safe_size(path)
            files.append(_Entry(name=name, path=path, is_dir=False, size_bytes=size))
        dirs.sort(key=lambda e: e.name.casefold())
        files.sort(key=lambda e: e.name.casefold())
        yield from dirs
        yield from files

    def _passes_filter(self, path: Path) -> bool:
        if self._mode == "epub":
            return path.suffix.lower() == ".epub"
        return False

    @staticmethod
    def _safe_size(path: Path) -> int | None:
        try:
            return path.stat().st_size
        except OSError:
            return None

    @staticmethod
    def _format_size(n: int | None) -> str:
        if n is None:
            return "—"
        if n < 1024:
            return f"{n} B"
        units = ("KB", "MB", "GB")
        size = float(n) / 1024.0
        for unit in units:
            if size < 1024.0:
                return f"{size:.1f} {unit}"
            size /= 1024.0
        return f"{size:.1f} TB"

    def _format_row(self, entry: _Entry) -> tuple[str, str, str]:
        if entry.is_parent:
            return ("↑", "[dim]..[/dim]", "[dim]parent[/dim]")
        if entry.is_dir:
            return ("📁", f"[b]{escape_markup(entry.name)}[/b]", "[dim]folder[/dim]")
        return ("📄", escape_markup(entry.name), self._format_size(entry.size_bytes))

    def _entry_at_cursor(self) -> _Entry | None:
        table = self.query_one("#fb-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            idx = int(table.cursor_row)
        except (AttributeError, TypeError, ValueError):
            return None
        if 0 <= idx < len(self._entries):
            return self._entries[idx]
        return None

    def _update_selection_label(self, entry: _Entry | None) -> None:
        self.query_one("#fb-selected", Static).update(self._selected_text(entry))

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        del event
        self._update_selection_label(self._entry_at_cursor())

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        del event
        self.action_activate()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "fb-cancel":
            self.action_cancel()
            return
        if button_id == "fb-activate":
            self.action_activate()
            return
        if button_id == "fb-pick-here":
            self._finish_with(self._current)
            return
        if button_id.startswith("fb-quick-"):
            label = button_id[len("fb-quick-") :]
            loc = self._find_location_by_label(label)
            if loc is not None:
                self._jump_to(loc.path)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "fb-path-input":
            return
        raw = event.input.value.strip()
        if not raw:
            return
        target = Path(raw).expanduser()
        if not target.is_absolute():
            target = (self._current / raw).resolve()
        else:
            target = target.resolve()
        if target.is_dir():
            self._jump_to(target)
            return
        if target.is_file() and self._passes_filter(target):
            self._finish_with(target)
            return
        self.app.bell()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_activate(self) -> None:
        entry = self._entry_at_cursor()
        if entry is None:
            if self._mode == "directory":
                self._finish_with(self._current)
            return
        if entry.is_dir:
            self._jump_to(entry.path)
            return
        if self._passes_filter(entry.path):
            self._finish_with(entry.path)

    def action_go_up(self) -> None:
        parent = self._current.parent
        if parent != self._current:
            self._jump_to(parent)

    def action_go_home(self) -> None:
        self._jump_to(Path.home())

    def action_refresh(self) -> None:
        self._reload_entries()

    def action_toggle_hidden(self) -> None:
        self._show_hidden = not self._show_hidden
        self._reload_entries()

    def action_quick_location(self, shortcut: str) -> None:
        for loc in self._locations:
            if loc.shortcut == shortcut:
                self._jump_to(loc.path)
                return

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _jump_to(self, path: Path) -> None:
        try:
            resolved = path.expanduser().resolve()
        except OSError:
            self.app.bell()
            return
        if not resolved.is_dir():
            self.app.bell()
            return
        self._current = resolved
        self._reload_entries()

    def _finish_with(self, path: Path) -> None:
        self.dismiss(path.resolve())

    def _find_location_by_label(self, raw_label: str) -> QuickLocation | None:
        needle = raw_label.strip().lower().replace(" ", "-")
        for loc in self._locations:
            if loc.label.lower().replace(" ", "-") == needle:
                return loc
        return None


__all__ = ["BrowserMode", "FileBrowserModal"]
