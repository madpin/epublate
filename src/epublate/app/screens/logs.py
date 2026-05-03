"""Logs screen — unified runtime audit + Python logger + LLM-call stream.

Resolves PRD §11's "where do log lines / errors live in the TUI?"
open question. The screen tails three sources merged by timestamp,
newest-first:

* **Events** — rows from the project's append-only ``event`` table.
  These survive restarts; they're the audit trail (PRD §6.4 / DB
  rule §3).
* **Logs** — :class:`logging.LogRecord`s from the in-memory
  :class:`epublate.app.log_buffer.RingBufferHandler`. These are
  ephemeral runtime context — useful for "what did the worker
  actually do five minutes ago" but explicitly NOT persisted (NFR-7
  / "no telemetry").
* **LLM calls** — rows from the project's ``llm_call`` table. Off
  by default because a long batch can produce thousands of rows
  and most of the time the curator only cares when something
  failed.

The three streams flow through the same :class:`textual.widgets.DataTable`
so the curator can scan everything at once. Filters live as
toggleable chips in the header strip (sources), a small group of
level shortcuts (``logs`` only), a substring search, and a coarse
time window. A detail pane below the table shows the full payload
for whatever row is highlighted.

Bindings follow the TUI keybinding rule (`.cursor/rules/tui-keybindings.mdc`):
``r`` Refresh, ``f`` cycles source filter, ``l`` cycles level, ``/``
opens the search input, ``q`` / ``Esc`` go back.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import textwrap
from dataclasses import dataclass
from typing import ClassVar, Literal

from rich.markup import escape as escape_markup
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Input, Static

from epublate.app.log_buffer import RingBufferHandler
from epublate.app.widgets import BatchStatusBar
from epublate.core.project import Project
from epublate.db import repo

LogSource = Literal["event", "log", "llm"]
"""Where a row originated. Mirrors the three filter chips."""

SourceFilter = Literal[
    "events+logs",
    "all",
    "events",
    "logs",
    "llm",
]
"""Toggleable source filters.

``events+logs`` is the default — LLM calls are noisy and the
curator usually only opens the screen to investigate something
specific. ``all`` is the explicit superset; ``events``, ``logs``,
``llm`` each scope down to one stream.
"""

_SOURCE_FILTERS: tuple[SourceFilter, ...] = (
    "events+logs",
    "all",
    "events",
    "logs",
    "llm",
)

LevelFilter = Literal["all", "warning+", "error+"]
"""Severity slider for the ``log`` stream.

Has no effect on events / llm rows — those don't carry a level.
``warning+`` and ``error+`` mean "WARNING and worse" / "ERROR and
worse" respectively, matching the standard ``logging`` thresholds.
"""

_LEVEL_FILTERS: tuple[LevelFilter, ...] = ("all", "warning+", "error+")

TimeFilter = Literal["all", "24h", "today"]
"""Time window applied uniformly across all sources."""

_TIME_FILTERS: tuple[TimeFilter, ...] = ("all", "24h", "today")

_PREVIEW_LEN = 80


@dataclass(slots=True, frozen=True)
class LogRow:
    """One unified row across the three streams.

    The detail pane needs the original record / row to render the
    full payload, so we keep a typed reference alongside the
    formatted columns instead of stringifying everything upfront.
    """

    source: LogSource
    timestamp: float
    level: str
    kind: str
    message: str
    payload: object
    """Original :class:`logging.LogRecord`, :class:`repo.EventRow`,
    or :class:`repo.LLMCallRow` — used by the detail pane only."""


class LogsScreen(Screen[None]):
    """Tailing log viewer with source/level/time filters.

    Read-only by design — the screen never mutates state, it's a
    pure observer over the App's runtime context.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("f", "cycle_source", "Source", show=True),
        Binding("l", "cycle_level", "Level", show=True),
        Binding("t", "cycle_time", "Time", show=True),
        Binding("slash", "focus_search", "Search", show=True),
        Binding("escape", "clear_search_or_back", "Back", show=False),
        Binding("q", "app.pop_screen", "Back", show=True),
    ]

    DEFAULT_CSS = """
    LogsScreen #logs-body {
        height: 1fr;
    }
    LogsScreen #logs-filters {
        height: auto;
        padding: 0 1;
        background: $boost;
    }
    LogsScreen #logs-search {
        height: 3;
        padding: 0 1;
    }
    LogsScreen #logs-table-wrap {
        height: 2fr;
    }
    LogsScreen #logs-table {
        height: 1fr;
        border: round $primary;
    }
    LogsScreen #logs-detail {
        height: 1fr;
        padding: 1 2;
        border: round $primary;
        overflow-y: auto;
    }
    LogsScreen #logs-status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    """

    source_filter: reactive[SourceFilter] = reactive[SourceFilter]("events+logs")
    level_filter: reactive[LevelFilter] = reactive[LevelFilter]("all")
    time_filter: reactive[TimeFilter] = reactive[TimeFilter]("24h")

    def __init__(
        self,
        project: Project,
        *,
        log_buffer: RingBufferHandler | None = None,
    ) -> None:
        super().__init__()
        self._project = project
        # Caller-injected for tests; otherwise we lazy-resolve from
        # the App on mount so plain ``LogsScreen(project)`` works.
        self._log_buffer = log_buffer
        self._rows: list[LogRow] = []
        self._row_index: dict[str, int] = {}
        # Set by the search input; ``None`` means no filter applied.
        # Reactive so the search submit can re-render in one call.
        self._needle: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="logs-body"):
            yield Static(self._filter_line(), id="logs-filters", markup=True)
            with Horizontal(id="logs-search"):
                yield Input(
                    placeholder="Substring search (regex not supported)…",
                    id="logs-search-input",
                )
            with Vertical(id="logs-table-wrap"):
                table: DataTable[str] = DataTable(
                    id="logs-table",
                    zebra_stripes=True,
                    cursor_type="row",
                )
                table.add_columns("When", "Source", "Level", "Kind/Logger", "Message")
                yield table
            yield Static(
                "(select a row for details)",
                id="logs-detail",
                markup=True,
            )
            yield Static("Ready.", id="logs-status")
        yield BatchStatusBar(id="logs-batch-status")
        yield Footer()

    def on_mount(self) -> None:
        if self._log_buffer is None:
            buf = getattr(self.app, "log_buffer", None)
            if isinstance(buf, RingBufferHandler):
                self._log_buffer = buf
        self._refresh()

    # ------------- state helpers -------------

    def _refresh(self) -> None:
        self._rows = self._build_rows()
        self._render_table()

    def _build_rows(self) -> list[LogRow]:
        rows: list[LogRow] = []
        if self._show_events():
            for ev in repo.list_events(self._project.engine, self._project.project_id):
                if ev.ts is None:
                    continue
                rows.append(
                    LogRow(
                        source="event",
                        timestamp=float(ev.ts),
                        level="",
                        kind=ev.kind,
                        message=_summarize_event_payload(ev),
                        payload=ev,
                    )
                )
        if self._show_logs() and self._log_buffer is not None:
            for rec in self._log_buffer.records():
                rows.append(
                    LogRow(
                        source="log",
                        timestamp=rec.created,
                        level=rec.levelname,
                        kind=rec.name,
                        message=getattr(rec, "message", None) or rec.getMessage(),
                        payload=rec,
                    )
                )
        if self._show_llm():
            for call in repo.list_llm_calls(
                self._project.engine,
                self._project.project_id,
                limit=500,
                descending=True,
            ):
                rows.append(
                    LogRow(
                        source="llm",
                        timestamp=float(call.created_at),
                        level="",
                        kind=f"{call.purpose}:{call.model}",
                        message=_summarize_llm_call(call),
                        payload=call,
                    )
                )
        rows.sort(key=lambda r: r.timestamp, reverse=True)
        return rows

    def _show_events(self) -> bool:
        return self.source_filter in {"events+logs", "all", "events"}

    def _show_logs(self) -> bool:
        return self.source_filter in {"events+logs", "all", "logs"}

    def _show_llm(self) -> bool:
        return self.source_filter in {"all", "llm"}

    def _level_threshold(self) -> int:
        if self.level_filter == "warning+":
            return logging.WARNING
        if self.level_filter == "error+":
            return logging.ERROR
        return logging.NOTSET

    def _time_window_start(self) -> float | None:
        now = dt.datetime.now(tz=dt.UTC)
        if self.time_filter == "24h":
            return (now - dt.timedelta(hours=24)).timestamp()
        if self.time_filter == "today":
            midnight = dt.datetime(now.year, now.month, now.day, tzinfo=dt.UTC)
            return midnight.timestamp()
        return None

    def _matches_filters(self, row: LogRow) -> bool:
        window = self._time_window_start()
        if window is not None and row.timestamp < window:
            return False
        if row.source == "log":
            level_no = logging.getLevelName(row.level)
            if isinstance(level_no, int) and level_no < self._level_threshold():
                return False
        if self._needle:
            haystack = f"{row.kind} {row.message} {row.level}".lower()
            if self._needle.lower() not in haystack:
                return False
        return True

    def _render_table(self) -> None:
        table = self.query_one("#logs-table", DataTable)
        table.clear()
        self._row_index.clear()
        visible = [r for r in self._rows if self._matches_filters(r)]
        for i, row in enumerate(visible):
            key = f"{row.source}:{i}:{int(row.timestamp)}"
            row_key = table.add_row(
                _fmt_ts(row.timestamp),
                row.source,
                row.level,
                escape_markup(_truncate(row.kind, width=24)),
                escape_markup(_truncate(row.message, width=_PREVIEW_LEN)),
                key=key,
            )
            self._row_index[str(row_key)] = i
        self.query_one("#logs-filters", Static).update(self._filter_line())
        self._set_status(
            f"sources={self.source_filter} · level={self.level_filter} · "
            f"time={self.time_filter} · "
            f"{len(visible)} of {len(self._rows)} shown"
        )
        self._render_detail()

    def _render_detail(self) -> None:
        detail = self.query_one("#logs-detail", Static)
        row = self._highlighted_row()
        if row is None:
            detail.update("(select a row for details)")
            return
        detail.update(_render_row_detail(row))

    def _highlighted_row(self) -> LogRow | None:
        table = self.query_one("#logs-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        idx = self._row_index.get(str(row_key))
        if idx is None:
            return None
        visible = [r for r in self._rows if self._matches_filters(r)]
        if idx < 0 or idx >= len(visible):
            return None
        return visible[idx]

    def _filter_line(self) -> str:
        chips = []
        for source in _SOURCE_FILTERS:
            mark = "[b reverse]" if source == self.source_filter else "[dim]"
            chips.append(f"{mark}{source}[/]")
        chips_str = "  ".join(chips)
        return (
            f"[b]src:[/b] {chips_str}    "
            f"[b]lvl:[/b] {self.level_filter}    "
            f"[b]time:[/b] {self.time_filter}    "
            f"[dim](r refresh · f source · l level · t time · / search)[/dim]"
        )

    def _set_status(self, text: str) -> None:
        self.query_one("#logs-status", Static).update(text)

    # ------------- actions -------------

    def action_refresh(self) -> None:
        self._refresh()

    def action_cycle_source(self) -> None:
        idx = (
            _SOURCE_FILTERS.index(self.source_filter)
            if self.source_filter in _SOURCE_FILTERS
            else 0
        )
        self.source_filter = _SOURCE_FILTERS[(idx + 1) % len(_SOURCE_FILTERS)]
        self._refresh()

    def action_cycle_level(self) -> None:
        idx = (
            _LEVEL_FILTERS.index(self.level_filter)
            if self.level_filter in _LEVEL_FILTERS
            else 0
        )
        self.level_filter = _LEVEL_FILTERS[(idx + 1) % len(_LEVEL_FILTERS)]
        self._render_table()

    def action_cycle_time(self) -> None:
        idx = (
            _TIME_FILTERS.index(self.time_filter)
            if self.time_filter in _TIME_FILTERS
            else 0
        )
        self.time_filter = _TIME_FILTERS[(idx + 1) % len(_TIME_FILTERS)]
        self._render_table()

    def action_focus_search(self) -> None:
        self.query_one("#logs-search-input", Input).focus()

    def action_clear_search_or_back(self) -> None:
        # ``Esc`` is overloaded here: in the search input it should
        # clear the needle and unfocus; everywhere else it should
        # behave like ``q`` and pop. Without this overload the curator
        # has to press Esc twice to dismiss a typo.
        try:
            search = self.query_one("#logs-search-input", Input)
        except Exception:
            self.app.pop_screen()
            return
        if search.has_focus and search.value:
            search.value = ""
            self._needle = None
            self._render_table()
            return
        self.app.pop_screen()

    @on(Input.Submitted, "#logs-search-input")
    def _on_search_submitted(self, event: Input.Submitted) -> None:
        # Substring search rather than regex so the curator can paste
        # the literal failing model name / segment id without escaping
        # special characters. Empty submit clears the filter.
        del event
        value = self.query_one("#logs-search-input", Input).value.strip()
        self._needle = value or None
        self._render_table()

    @on(Input.Changed, "#logs-search-input")
    def _on_search_changed(self, event: Input.Changed) -> None:
        # Live search: re-render on each keystroke so the curator sees
        # the table shrink as they type. The buffer is small enough
        # that this is cheap (<2k rows), and it matches the UX of the
        # Inbox and Glossary filters.
        del event
        value = self.query_one("#logs-search-input", Input).value.strip()
        self._needle = value or None
        self._render_table()

    @on(DataTable.RowHighlighted)
    def _on_row_highlighted(self, _message: DataTable.RowHighlighted) -> None:
        self._render_detail()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fmt_ts(ts: float) -> str:
    try:
        return dt.datetime.fromtimestamp(ts, tz=dt.UTC).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return str(int(ts))


def _truncate(text: str, *, width: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= width:
        return text
    return text[: max(1, width - 1)] + "…"


def _summarize_event_payload(ev: repo.EventRow) -> str:
    """One-line summary of an event row for the table cell.

    The detail pane still renders the full ``payload`` JSON; this is
    just what fits in the message column. We don't import the
    Inbox's ``_summarize_event`` to avoid a screen-to-screen import
    coupling — the LogsScreen is supposed to surface *every* event
    kind, not just the curated alert subset.
    """

    payload = ev.payload or {}
    if not payload:
        return ev.kind
    # Pull a couple of useful keys when we recognise them; otherwise
    # render a compact key=val list.
    parts: list[str] = []
    for key in ("error", "reason", "source_term", "segment_id", "model"):
        if key in payload:
            parts.append(f"{key}={payload[key]}")
    if parts:
        return ", ".join(str(p) for p in parts)
    return ", ".join(f"{k}={v}" for k, v in list(payload.items())[:4])


def _summarize_llm_call(call: repo.LLMCallRow) -> str:
    """One-line summary of an llm_call row for the table cell."""

    pt = call.prompt_tokens or 0
    ct = call.completion_tokens or 0
    cost = call.cost_usd or 0.0
    cache = "(cache)" if call.cache_hit else ""
    return f"{pt}p+{ct}c tok · ${cost:.4f} {cache}".strip()


def _render_row_detail(row: LogRow) -> str:
    """Render the highlighted row's full payload for the detail pane."""

    when = dt.datetime.fromtimestamp(row.timestamp, tz=dt.UTC).isoformat(
        timespec="seconds"
    )
    head = (
        f"[b]{row.source.upper()}[/b]   [dim]{escape_markup(when)}[/dim]   "
        f"[yellow]{escape_markup(row.kind)}[/yellow]"
    )
    if row.source == "event":
        ev: repo.EventRow = row.payload  # type: ignore[assignment]
        body = json.dumps(ev.payload, indent=2, default=str, ensure_ascii=False)
        return (
            f"{head}\n\n[b]payload[/b]\n{escape_markup(body)}"
            if ev.payload
            else f"{head}\n\n(empty payload)"
        )
    if row.source == "log":
        rec: logging.LogRecord = row.payload  # type: ignore[assignment]
        msg = rec.getMessage()
        out = [
            f"{head}",
            "",
            f"[b]message[/b]\n{escape_markup(msg)}",
        ]
        if rec.exc_info:
            try:
                # ``rec.exc_text`` is normally lazy-formatted by the
                # default formatter; do it ourselves so the detail
                # pane shows the traceback even when no formatter has
                # touched the record yet.
                if rec.exc_text is None:
                    fmt = logging.Formatter()
                    rec.exc_text = fmt.formatException(rec.exc_info)
            except Exception:
                rec.exc_text = repr(rec.exc_info)
            if rec.exc_text:
                out.append(f"\n[b red]traceback[/b red]\n{escape_markup(rec.exc_text)}")
        return "\n".join(out)
    if row.source == "llm":
        call: repo.LLMCallRow = row.payload  # type: ignore[assignment]
        body_parts = [
            f"{head}",
            "",
            f"[b]model[/b]    {call.model}",
            f"[b]purpose[/b]  {call.purpose}",
            f"[b]segment[/b]  {call.segment_id or '(n/a)'}",
            f"[b]tokens[/b]   {call.prompt_tokens or 0} prompt + "
            f"{call.completion_tokens or 0} completion",
            f"[b]cost[/b]     ${call.cost_usd or 0.0:.4f}",
            f"[b]cache[/b]    {'hit' if call.cache_hit else 'miss'}",
        ]
        if call.request_json:
            body_parts.append(
                "\n[b]request[/b]\n"
                + escape_markup(_truncate_block(call.request_json, max_chars=2000))
            )
        if call.response_json:
            body_parts.append(
                "\n[b]response[/b]\n"
                + escape_markup(_truncate_block(call.response_json, max_chars=2000))
            )
        return "\n".join(body_parts)
    return f"{head}\n\n{escape_markup(row.message)}"


def _truncate_block(text: str, *, max_chars: int) -> str:
    """Wrap + cap a JSON / text block for the detail pane."""

    if len(text) > max_chars:
        text = text[:max_chars] + "\n… (truncated)"
    # The terminal widget will hard-wrap, but very long single lines
    # (a stringified prompt) read better with a soft wrap first.
    return "\n".join(
        textwrap.fill(line, width=120, replace_whitespace=False)
        for line in text.splitlines() or [text]
    )


__all__ = [
    "LogRow",
    "LogSource",
    "LogsScreen",
    "SourceFilter",
]
