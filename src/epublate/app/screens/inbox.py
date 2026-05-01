"""Inbox screen — curator triage queue (PRD §4.6 / M4).

The Inbox merges three streams the curator needs to act on:

* **Flagged segments.** Translations that violated a ``locked``
  glossary entry (segment status = ``flagged``). Pressing ``Enter``
  jumps to the Reader on that segment.
* **Proposed glossary entries.** Auto-proposed by the translator
  pipeline (``status = proposed``); ``Enter`` opens the Glossary
  screen so the curator can promote / reject.
* **Alerts.** Curator-relevant events from the append-only ``event``
  log (batch paused / completed, budget changed, …).

All three live in one ``DataTable`` with a ``Kind`` column so the
curator can scan everything at once; ``f`` cycles a filter so they can
focus on a single stream when needed.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import ClassVar, Literal

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.reactive import reactive
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from epublate.app.widgets.cost_meter import CostMeter
from epublate.core.project import Project
from epublate.core.stats import (
    ProjectStats,
    compute_stats,
    flagged_segments,
    recent_alerts,
)
from epublate.db import repo
from epublate.glossary.models import GlossaryEntryWithAliases
from epublate.llm.base import LLMProvider
from epublate.llm.factory import build_provider

ProviderFactory = Callable[[], LLMProvider]
InboxKind = Literal["flagged", "proposed", "alert"]
FilterValue = Literal["all", "flagged", "proposed", "alert"]
_FILTERS: tuple[FilterValue, ...] = ("all", "flagged", "proposed", "alert")

DEFAULT_MODEL_FALLBACK = "gpt-5-mini"
_PREVIEW_LEN = 60


@dataclass(slots=True, frozen=True)
class InboxRow:
    """One row in the unified Inbox table.

    ``payload_id`` carries the destination identifier:

    * for ``flagged``: the segment id (so ``Enter`` can navigate).
    * for ``proposed``: the glossary entry id.
    * for ``alert``: the event id (no destination jump; informational).
    """

    kind: InboxKind
    label: str
    detail: str
    timestamp: int | None
    payload_id: str | None


class InboxScreen(Screen[None]):
    """Curator queue (PRD §4.6 / M4)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("enter", "jump", "Open", show=True),
        Binding("f", "cycle_filter", "Filter", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("j", "next_row", "Next", show=False),
        Binding("k", "prev_row", "Prev", show=False),
        Binding("q", "app.pop_screen", "Back", show=True),
        # ``escape`` mirrors ``q`` so curators can back out with the
        # universal "cancel" key; hidden from the footer to keep the
        # binding bar concise.
        Binding("escape", "app.pop_screen", "Back", show=False),
    ]

    DEFAULT_CSS = """
    InboxScreen #inbox-body {
        height: 1fr;
    }
    InboxScreen #inbox-table {
        height: 1fr;
        border: round $primary;
    }
    InboxScreen #inbox-cost {
        height: 1;
        padding: 0 1;
    }
    InboxScreen #inbox-status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    """

    filter_value: reactive[FilterValue] = reactive[FilterValue]("all")

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
        self._default_model = default_model or DEFAULT_MODEL_FALLBACK
        self._rows: list[InboxRow] = []
        self._row_index: dict[str, int] = {}

    @property
    def rows(self) -> list[InboxRow]:
        return self._rows

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with Vertical(id="inbox-body"):
            yield CostMeter(id="inbox-cost")
            table: DataTable[str] = DataTable(
                id="inbox-table", zebra_stripes=True, cursor_type="row"
            )
            table.add_columns("Kind", "When", "What", "Detail")
            yield table
            yield Static("Ready.", id="inbox-status")
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    # ------------- state helpers -------------

    def _refresh(self) -> None:
        self._rows = self._build_rows()
        stats = compute_stats(self._project.engine, self._project.project_id)
        self._render_table(stats=stats)

    def _build_rows(self) -> list[InboxRow]:
        rows: list[InboxRow] = []

        for seg in flagged_segments(
            self._project.engine, project_id=self._project.project_id
        ):
            preview = _truncate(seg.source_text)
            rows.append(
                InboxRow(
                    kind="flagged",
                    label=f"segment {seg.idx}",
                    detail=preview,
                    timestamp=None,
                    payload_id=seg.id,
                )
            )

        proposed = repo.list_glossary_entries(
            self._project.engine,
            self._project.project_id,
            status="proposed",
        )
        for entry in proposed:
            rows.append(
                InboxRow(
                    kind="proposed",
                    label=entry.source_term,
                    detail=f"type={entry.entry.type}",
                    timestamp=entry.entry.created_at or None,
                    payload_id=entry.id,
                )
            )

        alerts = recent_alerts(
            self._project.engine,
            project_id=self._project.project_id,
            limit=20,
        )
        for ev in alerts:
            rows.append(
                InboxRow(
                    kind="alert",
                    label=ev.kind,
                    detail=_summarize_event(ev),
                    timestamp=ev.ts,
                    payload_id=str(ev.id) if ev.id is not None else None,
                )
            )
        return rows

    def _render_table(self, *, stats: ProjectStats | None = None) -> None:
        table = self.query_one("#inbox-table", DataTable)
        table.clear()
        self._row_index.clear()
        visible = [r for r in self._rows if self._matches_filter(r)]
        for i, row in enumerate(visible):
            when = _fmt_ts(row.timestamp)
            row_key = table.add_row(
                row.kind,
                when,
                row.label,
                row.detail,
                key=f"{row.kind}:{row.payload_id}:{i}",
            )
            self._row_index[str(row_key)] = i

        meter = self.query_one("#inbox-cost", CostMeter)
        if stats is not None:
            meter.update_values(
                spend_usd=stats.spend_usd,
                budget_usd=stats.budget_usd,
            )

        flagged_count = sum(1 for r in self._rows if r.kind == "flagged")
        proposed_count = sum(1 for r in self._rows if r.kind == "proposed")
        alert_count = sum(1 for r in self._rows if r.kind == "alert")
        self._set_status(
            f"flagged={flagged_count}, proposed={proposed_count}, "
            f"alerts={alert_count} — filter: {self.filter_value} "
            f"({len(visible)} shown)"
        )

    def _matches_filter(self, row: InboxRow) -> bool:
        if self.filter_value == "all":
            return True
        return row.kind == self.filter_value

    def _set_status(self, text: str) -> None:
        self.query_one("#inbox-status", Static).update(text)

    def _highlighted_row(self) -> InboxRow | None:
        table = self.query_one("#inbox-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        except Exception:
            return None
        idx = self._row_index.get(str(row_key))
        if idx is None:
            return None
        visible = [r for r in self._rows if self._matches_filter(r)]
        if idx < 0 or idx >= len(visible):
            return None
        return visible[idx]

    # ------------- actions -------------

    def action_refresh(self) -> None:
        self._refresh()

    def action_next_row(self) -> None:
        table = self.query_one("#inbox-table", DataTable)
        if table.row_count == 0:
            return
        table.action_cursor_down()

    def action_prev_row(self) -> None:
        table = self.query_one("#inbox-table", DataTable)
        if table.row_count == 0:
            return
        table.action_cursor_up()

    def action_cycle_filter(self) -> None:
        idx = _FILTERS.index(self.filter_value) if self.filter_value in _FILTERS else 0
        self.filter_value = _FILTERS[(idx + 1) % len(_FILTERS)]
        self._render_table()

    def action_jump(self) -> None:
        self._jump_to_highlighted()

    @on(DataTable.RowSelected)
    def _on_row_selected(self, message: DataTable.RowSelected) -> None:
        # The DataTable swallows ``enter`` to emit ``RowSelected`` instead
        # of bubbling it up to the screen binding; route both paths to
        # the same handler so ``j/k + enter`` and ``click`` both work.
        del message
        self._jump_to_highlighted()

    def _jump_to_highlighted(self) -> None:
        row = self._highlighted_row()
        if row is None:
            return
        if row.kind == "flagged" and row.payload_id is not None:
            self._open_reader_at_segment(row.payload_id)
            return
        if row.kind == "proposed" and row.payload_id is not None:
            self._open_glossary(highlight_entry_id=row.payload_id)
            return
        # alerts are informational; no jump destination.
        self._set_status(f"Alert: {row.detail}")

    def _open_reader_at_segment(self, segment_id: str) -> None:
        from epublate.app.screens.reader import ReaderScreen

        seg = repo.get_segment(self._project.engine, segment_id)
        if seg is None:
            self._set_status(f"Segment not found: {segment_id}")
            return
        chapters = repo.list_chapters(self._project.engine, self._project.project_id)
        translatable: list[repo.ChapterRow] = []
        for chap in chapters:
            if repo.list_segments(self._project.engine, chap.id):
                translatable.append(chap)
        chapter_idx = next(
            (i for i, c in enumerate(translatable) if c.id == seg.chapter_id),
            0,
        )
        sibling = repo.list_segments(self._project.engine, seg.chapter_id)
        segment_idx = next((i for i, s in enumerate(sibling) if s.id == seg.id), 0)
        screen = ReaderScreen(
            self._project,
            provider_factory=self._provider_factory,
            model=self._default_model,
        )
        screen.chapter_idx = chapter_idx
        screen.segment_idx = segment_idx
        self.app.push_screen(screen, self._on_child_screen_closed)

    def _open_glossary(self, *, highlight_entry_id: str) -> None:
        from epublate.app.screens.glossary import GlossaryScreen

        screen = GlossaryScreen(self._project)
        # No first-class deep-link API on the Glossary screen yet; opening
        # it from the Inbox already filters the curator's attention because
        # the proposed entry is fresh in their mind. We could add a
        # `highlight=` parameter in M5 if the workflow asks for it.
        del highlight_entry_id  # reserved
        self.app.push_screen(screen, self._on_child_screen_closed)

    def _on_child_screen_closed(self, _result: object) -> None:
        self._refresh()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _truncate(text: str, *, width: int = _PREVIEW_LEN) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


def _fmt_ts(ts: int | None) -> str:
    if ts is None:
        return ""
    try:
        return dt.datetime.fromtimestamp(ts, tz=dt.UTC).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return str(ts)


def _summarize_event(ev: repo.EventRow) -> str:
    """Compact human-readable summary of an alert event."""

    payload = ev.payload
    if ev.kind == "batch.completed":
        return (
            f"translated={payload.get('translated', 0)}, "
            f"cached={payload.get('cached', 0)}, "
            f"flagged={payload.get('flagged', 0)}, "
            f"cost=${float(payload.get('cost_usd', 0.0)):.4f}"
        )
    if ev.kind == "batch.paused":
        reason = payload.get("reason", "budget cap reached")
        return str(reason)
    if ev.kind == "batch.started":
        return f"started ({payload.get('segment_count', '?')} segments)"
    if ev.kind == "batch.segment_failed":
        err = str(payload.get("error", ""))
        return f"segment failed: {_truncate(err, width=80)}"
    if ev.kind == "segment.translation_flagged":
        return f"segment {payload.get('segment_id', '?')} flagged"
    if ev.kind == "segment.translation_failed":
        return f"segment {payload.get('segment_id', '?')} parse failure"
    if ev.kind == "entity.proposed":
        return f"new proposed entry: {payload.get('source_term', '?')}"
    if ev.kind == "project.budget_changed":
        new_b = payload.get("new_budget_usd")
        return f"budget set to {new_b}" if new_b is not None else "budget cleared"
    if ev.kind == "glossary.cascaded":
        return (
            f"cascaded {payload.get('affected_count', 0)} segments for "
            f"{payload.get('source_term', '?')!r}"
        )
    if ev.kind == "intake.started":
        return f"intake started ({payload.get('segment_count', '?')} segments)"
    if ev.kind == "intake.completed":
        return (
            f"intake done: {payload.get('chunks', 0)} chunks, "
            f"{payload.get('proposed_count', 0)} proposed, "
            f"${float(payload.get('cost_usd', 0.0)):.4f}"
        )
    if ev.kind == "batch.pre_pass_started":
        return f"pre-pass started ({payload.get('chunk_count', '?')} chunks)"
    if ev.kind == "batch.pre_pass_completed":
        return (
            f"pre-pass done: {payload.get('chunks', 0)} chunks, "
            f"{payload.get('proposed_count', 0)} proposed"
        )
    if ev.kind == "entity.extracted":
        return (
            f"extractor: {payload.get('entities', 0)} entities, "
            f"{payload.get('proposed', 0)} proposed"
        )
    if ev.kind == "entity.extract_failed":
        return f"extractor failed (model={payload.get('model', '?')})"
    return ""


def _resolve_proposed_entry(
    engine: object, *, entry_id: str
) -> GlossaryEntryWithAliases | None:
    """Reserved for a future deep-link from Inbox alert → Glossary."""

    return None


__all__ = [
    "InboxKind",
    "InboxRow",
    "InboxScreen",
    "ProviderFactory",
]
