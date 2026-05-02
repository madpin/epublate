"""Lore Books screen — peer of ProjectsScreen for the Lore Book primitive.

Lists every Lore Book under the configured library (PRD §4.3 /
F-LB-10), lets the curator create a new Lore Book or open one by
path, and pushes :class:`LoreBookDashboardScreen` for the chosen book.
The cascading recents store is intentionally separate from the
``projects/`` recents — Lore Books and Projects have different
identities and the curator usually flips between two short lists, not
one mixed firehose.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Center, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
)

from epublate.app.branding import ICON_BULLET
from epublate.lore import (
    LoreBook,
    default_library_dir,
    iter_library_lore_books,
)
from epublate.lore.library import LoreBookHandle

_logger = logging.getLogger(__name__)

_HERO_HINT = (
    f"  {ICON_BULLET} [b]n[/b] new lore   "
    f"{ICON_BULLET} [b]o[/b] open by path   "
    f"{ICON_BULLET} [b]enter[/b] open selected"
)

_EMPTY_BODY = (
    "[b]Lore Books are portable lore bibles.[/b]\n\n"
    "Create one to share canonical proper-noun translations across\n"
    "every book in a series — attach it to as many projects as you like.\n\n"
    "  [b yellow]n[/]  Create a [b]new lore book[/] in your library\n"
    "  [b yellow]o[/]  Open an existing lore book [b]folder[/] by path\n\n"
    "  [dim]Library lives at:[/] [b]{library}[/]"
)


# ---------------------------------------------------------------------------
# Modals
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class NewLoreBookResult:
    """Outcome of :class:`NewLoreBookModal`.

    ``out_dir`` is the directory the curator picked; the screen
    creates the Lore Book there. We keep the modal pure (no DB
    side-effects) so cancel-after-typing doesn't leave artifacts.

    ``from_project_dir`` is an optional path to an existing
    translation project whose curated glossary should bootstrap the
    new Lore Book (PRD F-LB-10). The destination is empty by
    construction, so no conflict resolution is needed at create time.
    """

    out_dir: Path
    name: str
    source_lang: str
    target_lang: str
    description: str | None
    from_project_dir: Path | None = None


class NewLoreBookModal(ModalScreen[NewLoreBookResult | None]):
    """Tiny form: name, source/target lang, library destination."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "Create", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    NewLoreBookModal {
        align: center middle;
    }
    NewLoreBookModal #lore-new-box {
        width: 80%;
        max-width: 90;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    NewLoreBookModal Label {
        padding: 0 0 0 0;
    }
    NewLoreBookModal Input {
        width: 1fr;
    }
    NewLoreBookModal #lore-new-help {
        height: auto;
        color: $text-muted;
        padding: 1 0 0 0;
    }
    """

    def __init__(self, *, library_dir: Path | None = None) -> None:
        super().__init__()
        self._library_dir = library_dir or default_library_dir()

    def compose(self) -> ComposeResult:
        with Vertical(id="lore-new-box"):
            yield Label("[b]New Lore Book[/b]", markup=True)
            yield Label("Name:")
            yield Input(value="", id="lore-new-name", placeholder="e.g. Witcher Lore")
            yield Label("Source language (BCP-47):")
            yield Input(value="en", id="lore-new-source-lang")
            yield Label("Target language (BCP-47):")
            yield Input(value="pt", id="lore-new-target-lang")
            yield Label("Description (optional):")
            yield Input(value="", id="lore-new-description")
            yield Label("Import glossary from project (optional):")
            yield Input(
                value="",
                id="lore-new-from-project",
                placeholder="/path/to/book1.epublate",
            )
            yield Static(
                f"Will be created under [b]{self._library_dir}[/b].\n"
                "Leave the import field empty for an empty Lore Book; otherwise "
                "the new book is bootstrapped from that project's glossary.\n"
                "Press [b]Ctrl+S[/b] to create, [b]Esc[/b] to cancel.",
                id="lore-new-help",
                markup=True,
            )

    def on_mount(self) -> None:
        self.query_one("#lore-new-name", Input).focus()

    def action_save(self) -> None:
        name = self.query_one("#lore-new-name", Input).value.strip()
        source_lang = self.query_one("#lore-new-source-lang", Input).value.strip()
        target_lang = self.query_one("#lore-new-target-lang", Input).value.strip()
        description = self.query_one("#lore-new-description", Input).value.strip()
        from_raw = self.query_one("#lore-new-from-project", Input).value.strip()
        if not name or not source_lang or not target_lang:
            self.app.bell()
            return
        from_project_dir: Path | None = None
        if from_raw:
            candidate = Path(from_raw).expanduser()
            if not candidate.is_dir():
                self.app.bell()
                return
            from_project_dir = candidate.resolve()
        slug = _slugify(name) or "lore"
        out_dir = self._library_dir / f"{slug}.epublate-lore"
        self.dismiss(
            NewLoreBookResult(
                out_dir=out_dir,
                name=name,
                source_lang=source_lang,
                target_lang=target_lang,
                description=description or None,
                from_project_dir=from_project_dir,
            )
        )

    def action_cancel(self) -> None:
        self.dismiss(None)


@dataclass(slots=True, frozen=True)
class OpenLoreBookResult:
    """Outcome of :class:`OpenLoreBookModal`."""

    lore_dir: Path


class OpenLoreBookModal(ModalScreen[OpenLoreBookResult | None]):
    """Prompt the curator for a Lore Book directory."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "save", "Open", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    OpenLoreBookModal {
        align: center middle;
    }
    OpenLoreBookModal #lore-open-box {
        width: 80%;
        max-width: 90;
        height: auto;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    OpenLoreBookModal Input {
        width: 1fr;
    }
    OpenLoreBookModal #lore-open-help {
        color: $text-muted;
        padding: 1 0 0 0;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="lore-open-box"):
            yield Label("[b]Open Lore Book[/b]", markup=True)
            yield Label("Lore Book directory:")
            yield Input(
                value="",
                id="lore-open-path",
                placeholder="/path/to/whatever.epublate-lore",
            )
            yield Static(
                "Press [b]Ctrl+S[/b] to open, [b]Esc[/b] to cancel.",
                id="lore-open-help",
                markup=True,
            )

    def on_mount(self) -> None:
        self.query_one("#lore-open-path", Input).focus()

    def action_save(self) -> None:
        raw = self.query_one("#lore-open-path", Input).value.strip()
        if not raw:
            self.app.bell()
            return
        path = Path(raw).expanduser()
        if not path.is_dir():
            self.app.bell()
            return
        self.dismiss(OpenLoreBookResult(lore_dir=path.resolve()))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# Main screen
# ---------------------------------------------------------------------------


class LoreBooksScreen(Screen[None]):
    """List view + new/open flows for Lore Books."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("n", "new_lore_book", "New", show=True),
        Binding("o", "open_lore_book", "Open", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("q", "app.pop_screen", "Back", show=True),
        Binding("escape", "app.pop_screen", "Back", show=False),
    ]

    DEFAULT_CSS = """
    LoreBooksScreen {
        background: $background;
    }
    LoreBooksScreen #lore-books-hero {
        height: auto;
        padding: 1 2 0 2;
    }
    LoreBooksScreen #lore-books-hint {
        content-align: center middle;
        height: 1;
        padding: 0 0 1 0;
    }
    LoreBooksScreen #lore-books-summary {
        height: auto;
        padding: 1 2;
        margin: 0 2 0 2;
        border: round $accent;
        background: $panel;
    }
    LoreBooksScreen #lore-books-table-wrap {
        height: 1fr;
        padding: 0 2;
    }
    LoreBooksScreen #lore-books-table {
        height: 1fr;
        border: round $primary;
        background: $surface;
    }
    LoreBooksScreen #lore-books-empty-wrap {
        height: 1fr;
        padding: 0 2;
        align: center middle;
    }
    LoreBooksScreen #lore-books-empty {
        height: auto;
        width: 70;
        padding: 2 4;
        border: round $primary;
        background: $panel;
        content-align: center middle;
    }
    LoreBooksScreen #lore-books-status {
        dock: bottom;
        height: 1;
        padding: 0 2;
        background: $boost;
        color: $text-muted;
    }
    """

    COLUMN_KEYS = ("Name", "Languages", "Path")

    def __init__(
        self,
        *,
        library_dir: Path | None = None,
        provider_factory: object | None = None,
        helper_model: str | None = None,
    ) -> None:
        super().__init__()
        self._library_dir = library_dir or default_library_dir()
        # Optional plumbing the Dashboard needs to actually run an
        # ingest. Tests (and the demo CLI subcommand) can leave both
        # ``None``; the modal then surfaces a friendly "no provider"
        # status instead of throwing.
        self._provider_factory = provider_factory
        self._helper_model = helper_model
        self._handles: list[LoreBookHandle] = []
        self._active_book: LoreBook | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical():
            with Vertical(id="lore-books-hero"):
                yield Static(
                    "[b]Lore Books[/b]",
                    id="lore-books-title",
                    markup=True,
                )
                yield Static(_HERO_HINT, id="lore-books-hint", markup=True)
            yield Static(
                self._summary_line(),
                id="lore-books-summary",
                markup=True,
            )
            with Vertical(id="lore-books-table-wrap"):
                table: DataTable[str] = DataTable(
                    id="lore-books-table",
                    zebra_stripes=True,
                    cursor_type="row",
                )
                table.add_columns(*self.COLUMN_KEYS)
                yield table
            with Center(id="lore-books-empty-wrap"):
                yield Static(
                    _EMPTY_BODY.format(library=self._library_dir),
                    id="lore-books-empty",
                    markup=True,
                )
            yield Static(
                "Ready.",
                id="lore-books-status",
                markup=True,
            )
        yield Footer()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def on_mount(self) -> None:
        self._refresh_table()

    def on_unmount(self) -> None:
        self._close_active_book()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_refresh(self) -> None:
        self._refresh_table()
        self._set_status("Reloaded library.")

    def action_new_lore_book(self) -> None:
        modal = NewLoreBookModal(library_dir=self._library_dir)
        self.app.push_screen(modal, self._on_new_done)

    def action_open_lore_book(self) -> None:
        modal = OpenLoreBookModal()
        self.app.push_screen(modal, self._on_open_done)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        del event
        handle = self._selected_handle()
        if handle is None:
            return
        try:
            book = LoreBook.open(handle.lore_dir)
        except Exception as exc:
            self._set_status(f"Could not open {handle.name!r}: {exc}")
            return
        self._open_book(book)

    # ------------------------------------------------------------------
    # Modal callbacks
    # ------------------------------------------------------------------

    def _on_new_done(self, result: NewLoreBookResult | None) -> None:
        if result is None:
            self._set_status("New Lore Book canceled.")
            return
        try:
            book = LoreBook.create(
                out_dir=result.out_dir,
                name=result.name,
                source_lang=result.source_lang,
                target_lang=result.target_lang,
                description=result.description,
            )
        except Exception as exc:
            self._set_status(f"Could not create Lore Book: {exc}")
            return
        bootstrap_msg = ""
        if result.from_project_dir is not None:
            from epublate.lore import import_project_glossary

            try:
                summary = import_project_glossary(
                    book,
                    src_project_dir=result.from_project_dir,
                    policy="overwrite",
                )
                bootstrap_msg = (
                    f" Bootstrapped {summary.created} entries"
                    f" from {result.from_project_dir.name}."
                )
            except Exception as exc:
                _logger.exception("lore bootstrap failed")
                bootstrap_msg = f" Bootstrap failed: {exc}"
        self._refresh_table()
        self._set_status(f"Created Lore Book {book.name!r}.{bootstrap_msg}")
        self._open_book(book)

    def _on_open_done(self, result: OpenLoreBookResult | None) -> None:
        if result is None:
            self._set_status("Open canceled.")
            return
        try:
            book = LoreBook.open(result.lore_dir)
        except Exception as exc:
            self._set_status(f"Could not open Lore Book: {exc}")
            return
        self._refresh_table()
        self._open_book(book)

    # ------------------------------------------------------------------
    # Lore Book lifecycle
    # ------------------------------------------------------------------

    def _open_book(self, book: LoreBook) -> None:
        from epublate.app.screens.lore_book_dashboard import (
            LoreBookDashboardScreen,
        )

        self._close_active_book()
        self._active_book = book
        screen = LoreBookDashboardScreen(
            book,
            provider_factory=self._provider_factory,  # type: ignore[arg-type]
            helper_model=self._helper_model,
        )
        self.app.push_screen(screen, self._on_dashboard_closed)
        self._set_status(f"Opened {book.name!r}.")

    def _on_dashboard_closed(self, _result: object) -> None:
        self._close_active_book()
        self._refresh_table()
        self._set_status("Back to Lore Books.")

    def _close_active_book(self) -> None:
        if self._active_book is None:
            return
        try:
            self._active_book.close()
        except Exception:
            _logger.exception("error closing lore book")
        finally:
            self._active_book = None

    # ------------------------------------------------------------------
    # Table helpers
    # ------------------------------------------------------------------

    def _refresh_table(self) -> None:
        self._handles = list(_iter_books(self._library_dir))
        table = self.query_one("#lore-books-table", DataTable)
        table.clear()
        for handle in self._handles:
            row_summary = _summarize_handle(handle)
            table.add_row(
                f"[b]{handle.name}[/]",
                row_summary.languages,
                f"[dim]{handle.lore_dir}[/]",
                key=str(handle.lore_dir),
            )
        has_entries = bool(self._handles)
        self.query_one("#lore-books-empty-wrap").display = not has_entries
        self.query_one("#lore-books-table-wrap").display = has_entries
        self.query_one("#lore-books-summary", Static).update(self._summary_line())

    def _selected_handle(self) -> LoreBookHandle | None:
        table = self.query_one("#lore-books-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_index = int(table.cursor_row)
        except (AttributeError, TypeError, ValueError):
            return None
        if row_index < 0 or row_index >= len(self._handles):
            return None
        return self._handles[row_index]

    def _summary_line(self) -> str:
        n = len(self._handles)
        if n == 0:
            return (
                f"  [dim]No Lore Books in[/] [b]{self._library_dir}[/] [dim]yet — "
                f"press [b]n[/] to create one.[/]"
            )
        return (
            f"  [yellow][b]{n}[/][/] Lore Book{'s' if n != 1 else ''} "
            f"[dim]in[/] [b]{self._library_dir}[/]"
        )

    def _set_status(self, message: str) -> None:
        self.query_one("#lore-books-status", Static).update(message)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class _HandleSummary:
    languages: str


def _iter_books(library_dir: Path) -> Iterable[LoreBookHandle]:
    yield from iter_library_lore_books(library_dir)


def _summarize_handle(handle: LoreBookHandle) -> _HandleSummary:
    """Cheap one-line summary for the table without opening the DB.

    We deliberately don't open the SQLite engine here — the table
    renders on every refresh and the library may grow into the
    hundreds. The Dashboard screen opens the DB lazily when the
    curator picks a row.
    """

    return _HandleSummary(languages="(open to see)")


def _slugify(text: str) -> str:
    """ASCII-ish, URL-safe slug for the on-disk directory name."""

    cleaned = []
    last_was_dash = False
    for char in text.lower():
        if char.isalnum():
            cleaned.append(char)
            last_was_dash = False
        elif not last_was_dash and char in (" ", "-", "_"):
            cleaned.append("-")
            last_was_dash = True
    return "".join(cleaned).strip("-")


__all__ = [
    "LoreBooksScreen",
    "NewLoreBookModal",
    "NewLoreBookResult",
    "OpenLoreBookModal",
    "OpenLoreBookResult",
]
