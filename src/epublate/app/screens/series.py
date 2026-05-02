"""Series rollup view for a Lore Book (PRD F-LB-10).

The "series" view is intentionally lightweight: a Lore Book is the
spine of a series, and the curator wants to see — at a glance — which
translation projects currently attach this Lore Book and how each one
is doing. We surface aggregate progress, total spend, and per-book
status without ever writing to anything.

Project discovery is best-effort: we walk the recents store
(:class:`epublate.app.recents.RecentsStore`) since that's the
authoritative list of projects the curator has touched on this
machine. Projects attached from a different machine, or never opened
on this one, won't appear — adding a global "all known projects"
registry would be a bigger design change and isn't worth the surface
area for v1.

The screen is read-only: opening it never mutates anything, and we
dispose every per-project SQLAlchemy engine before returning so a
forgotten window doesn't keep WAL files pinned open.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Label, Static

from epublate.app.recents import RecentProject, RecentsStore
from epublate.core.project import Project
from epublate.core.stats import ProjectStats, compute_stats
from epublate.db import repo
from epublate.lore import LoreBook

_logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class _SeriesEntry:
    """One row in the Series view: a project that attaches this Lore Book."""

    recent: RecentProject
    mode: str
    priority: int
    stats: ProjectStats


class SeriesScreen(Screen[None]):
    """Read-only rollup of every project attaching one Lore Book."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("q", "app.pop_screen", "Back", show=True),
        Binding("escape", "app.pop_screen", "Back", show=False),
    ]

    DEFAULT_CSS = """
    SeriesScreen #series-body {
        height: 1fr;
    }
    SeriesScreen .panel {
        border: round $primary;
        padding: 1 2;
        margin: 0 1 1 1;
        background: $panel;
    }
    SeriesScreen .panel-title {
        text-style: bold;
        color: $primary;
    }
    SeriesScreen #series-status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $boost;
        color: $text-muted;
    }
    """

    def __init__(self, lore_book: LoreBook) -> None:
        super().__init__()
        self._lore_book = lore_book
        self._lore_path_resolved = str(Path(lore_book.lore_dir).resolve())

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="series-body"):
            yield Static(
                self._header_line(),
                id="series-header",
                classes="panel",
                markup=True,
            )
            with Vertical(id="series-summary-panel", classes="panel"):
                yield Label("Rollup", classes="panel-title")
                yield Static(
                    "(loading…)",
                    id="series-summary-body",
                    markup=True,
                )
            with Vertical(id="series-projects-panel", classes="panel"):
                yield Label("Attached projects", classes="panel-title")
                table: DataTable[str] = DataTable(
                    id="series-projects-table",
                    zebra_stripes=True,
                    cursor_type="row",
                )
                yield table
            yield Static("Ready.", id="series-status", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#series-projects-table", DataTable)
        if not table.columns:
            table.add_columns(
                "Project",
                "Langs",
                "Mode",
                "% translated",
                "% approved",
                "Spend $",
                "Last opened",
            )
        self._refresh()

    def action_refresh(self) -> None:
        self._refresh()
        self._set_status("Reloaded.")

    def _header_line(self) -> str:
        book = self._lore_book
        return (
            f"[b]Series rollup —[/b] [cyan]{book.name}[/cyan]   "
            f"[dim]·[/] [yellow]{book.source_lang}[/] → [yellow]{book.target_lang}[/]"
            f"   [dim]· lore path:[/] [b]{book.lore_dir}[/]"
        )

    def _refresh(self) -> None:
        entries = list(self._collect_entries())
        self._populate_table(entries)
        self._populate_summary(entries)

    def _collect_entries(self) -> Iterable[_SeriesEntry]:
        """Yield one entry per recent project that attaches this Lore Book.

        Projects whose folder no longer exists, whose DB doesn't open,
        or that don't have an ``attached_lore`` row matching this Lore
        Book are silently skipped. Errors are logged at debug level so
        a stale recents file never crashes the screen.
        """

        recents = RecentsStore.load()
        for recent in recents.entries:
            if not recent.exists():
                continue
            try:
                project = Project.open(recent.path)
            except Exception as exc:
                _logger.debug(
                    "series: skipping unopenable project %s: %s",
                    recent.path,
                    exc,
                )
                continue
            try:
                attached = repo.list_attached_lore(
                    project.engine, project_id=project.project_id
                )
                match = next(
                    (
                        a
                        for a in attached
                        if str(Path(a.lore_path).resolve()) == self._lore_path_resolved
                    ),
                    None,
                )
                if match is None:
                    continue
                stats = compute_stats(project.engine, project.project_id)
            except Exception as exc:
                _logger.debug("series: stats failed for %s: %s", recent.path, exc)
                continue
            finally:
                project.close()
            yield _SeriesEntry(
                recent=recent,
                mode=match.mode,
                priority=match.priority,
                stats=stats,
            )

    def _populate_table(self, entries: list[_SeriesEntry]) -> None:
        table = self.query_one("#series-projects-table", DataTable)
        table.clear()
        if not entries:
            return
        ordered = sorted(
            entries,
            key=lambda e: (-e.recent.last_opened, e.recent.name.casefold()),
        )
        for entry in ordered:
            stats = entry.stats
            translated_pct = (
                f"{stats.progress_ratio * 100:5.1f}%"
                if stats.segment_count
                else "  —  "
            )
            approved_ratio = (
                stats.approved_count / stats.segment_count
                if stats.segment_count
                else 0.0
            )
            approved_pct = (
                f"{approved_ratio * 100:5.1f}%" if stats.segment_count else "  —  "
            )
            spend_str = f"${stats.spend_usd:7.4f}"
            last_opened = (
                time.strftime(
                    "%Y-%m-%d %H:%M",
                    time.localtime(entry.recent.last_opened),
                )
                if entry.recent.last_opened
                else "—"
            )
            langs = f"{entry.recent.source_lang} → {entry.recent.target_lang}"
            table.add_row(
                entry.recent.name,
                langs,
                entry.mode,
                translated_pct,
                approved_pct,
                spend_str,
                last_opened,
            )

    def _populate_summary(self, entries: list[_SeriesEntry]) -> None:
        body = self.query_one("#series-summary-body", Static)
        if not entries:
            body.update(
                "[dim]No projects on this machine attach this Lore Book yet.\n"
                "Open a project, go to [b]s[/b] Settings → Lore Books, "
                "and attach this Lore Book to start rolling it up here.[/dim]"
            )
            return

        total_segments = sum(e.stats.segment_count for e in entries)
        total_translated = sum(e.stats.translated_count for e in entries)
        total_approved = sum(e.stats.approved_count for e in entries)
        total_spend = sum(e.stats.spend_usd for e in entries)
        writable = sum(1 for e in entries if e.mode == "writable")

        translated_pct = (
            (total_translated / total_segments) * 100 if total_segments else 0.0
        )
        approved_pct = (
            (total_approved / total_segments) * 100 if total_segments else 0.0
        )
        body.update(
            f"[b]{len(entries)}[/b] project(s)   "
            f"[dim]· {writable} writable, {len(entries) - writable} read-only[/]\n"
            f"[b]{total_translated}/{total_segments}[/b] segments translated "
            f"([cyan]{translated_pct:.1f}%[/cyan])   "
            f"[b]{total_approved}[/b] approved "
            f"([green]{approved_pct:.1f}%[/green])\n"
            f"Total spend: [yellow]${total_spend:.4f}[/yellow]"
        )

    def _set_status(self, text: str) -> None:
        self.query_one("#series-status", Static).update(text)


__all__ = ["SeriesScreen"]
