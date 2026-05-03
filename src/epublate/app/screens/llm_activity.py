"""Dedicated LLM activity screen — deep cost / token stats (PRD F-LLM-7).

The Dashboard's ``LLM activity`` panel is intentionally compact (only
the last few calls fit). When the curator wants to understand *where*
the budget is going — which model, which purpose, which segment — they
press ``L`` from the Dashboard to open this screen.

Sections, top-to-bottom:

* **Totals strip.** Calls, prompt / completion tokens, cache-hit rate,
  and total spend (with the project's budget line for context).
* **Per-model rollup.** One row per model slug with calls, tokens,
  cost, and a fraction-of-spend bar. Sorted by cost descending so the
  most expensive model is at the top.
* **Per-purpose rollup.** Same shape, grouped by
  :data:`epublate.core.pipeline.PURPOSE_TRANSLATE`,
  :data:`epublate.core.extractor.PURPOSE_EXTRACT`,
  :data:`epublate.core.style_sniff.PURPOSE_TONE_SNIFF`, etc. Tells the
  curator at a glance whether intake / cascade / translation is the
  dominant spender.
* **Recent calls table.** The latest N rows from ``llm_call`` with
  timestamp, model, purpose, segment id, tokens, cost, and a cache
  flag — same data the Dashboard panel surfaces, just unbounded.

Everything is read-only; the screen exists so the curator can
*understand* spend, not edit it (budget changes still go through the
Dashboard's ``BudgetModal``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import ClassVar

from rich.markup import escape as escape_markup
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Label, Static

from epublate.app.widgets import BatchStatusBar
from epublate.core.project import Project
from epublate.core.stats import (
    LLMActivityBreakdown,
    LLMActivityRow,
    ProjectStats,
    compute_llm_activity,
    compute_stats,
)
from epublate.db import repo

_MAX_RECENT_CALLS = 50


class LLMActivityScreen(Screen[None]):
    """Read-only deep-stats view over ``llm_call``.

    Pushed by the Dashboard's ``L`` binding. Mounting and tab-back are
    cheap because every aggregation is a single grouped SQL query
    (PRD NFR-2 / SQLite WAL).
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("r", "refresh", "Refresh", show=True),
        Binding("q", "app.pop_screen", "Back", show=True),
        # ``escape`` mirrors ``q`` for the universal "cancel" key but
        # is hidden from the footer to keep the binding bar concise.
        Binding("escape", "app.pop_screen", "Back", show=False),
    ]

    DEFAULT_CSS = """
    LLMActivityScreen #llm-body {
        height: 1fr;
        padding: 0 1;
    }
    LLMActivityScreen .panel {
        border: round $primary;
        padding: 1 2;
        margin: 0 0 1 0;
        height: auto;
    }
    LLMActivityScreen .panel-title {
        text-style: bold;
        color: $primary;
        padding: 0 0 1 0;
    }
    LLMActivityScreen #llm-totals {
        height: auto;
    }
    LLMActivityScreen #llm-by-model-table,
    LLMActivityScreen #llm-by-purpose-table {
        height: auto;
        max-height: 12;
    }
    LLMActivityScreen #llm-recent-table {
        height: 1fr;
        max-height: 24;
    }
    LLMActivityScreen #llm-status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    """

    def __init__(self, project: Project) -> None:
        super().__init__()
        self._project = project

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll(id="llm-body"):
            with Vertical(id="llm-totals", classes="panel"):
                yield Label("Totals", classes="panel-title")
                yield Static("(loading…)", id="llm-totals-text", markup=True)
            with Vertical(id="llm-by-model-panel", classes="panel"):
                yield Label("By model", classes="panel-title")
                model_table: DataTable[str] = DataTable(
                    id="llm-by-model-table",
                    zebra_stripes=True,
                    cursor_type="none",
                )
                yield model_table
            with Vertical(id="llm-by-purpose-panel", classes="panel"):
                yield Label("By purpose", classes="panel-title")
                purpose_table: DataTable[str] = DataTable(
                    id="llm-by-purpose-table",
                    zebra_stripes=True,
                    cursor_type="none",
                )
                yield purpose_table
            with Vertical(id="llm-recent-panel", classes="panel"):
                yield Label(
                    f"Recent calls (last {_MAX_RECENT_CALLS})",
                    classes="panel-title",
                )
                recent_table: DataTable[str] = DataTable(
                    id="llm-recent-table",
                    zebra_stripes=True,
                    cursor_type="row",
                )
                yield recent_table
        yield Static("Ready.", id="llm-status", markup=True)
        yield BatchStatusBar(id="llm-batch-status")
        yield Footer()

    def on_mount(self) -> None:
        model_table = self.query_one("#llm-by-model-table", DataTable)
        if not model_table.columns:
            model_table.add_columns(
                "Model", "Calls", "Cache", "In", "Out", "Total", "Cost", "Share"
            )
        purpose_table = self.query_one("#llm-by-purpose-table", DataTable)
        if not purpose_table.columns:
            purpose_table.add_columns(
                "Purpose", "Calls", "Cache", "In", "Out", "Total", "Cost", "Share"
            )
        recent_table = self.query_one("#llm-recent-table", DataTable)
        if not recent_table.columns:
            recent_table.add_columns(
                "When", "Model", "Purpose", "Segment", "In", "Out", "Cost", "Cache"
            )
        self._refresh()

    def action_refresh(self) -> None:
        self._refresh()
        self._set_status("Refreshed.")

    def _refresh(self) -> None:
        stats = compute_stats(self._project.engine, self._project.project_id)
        breakdown = compute_llm_activity(self._project.engine, self._project.project_id)
        recent = repo.list_llm_calls(
            self._project.engine,
            self._project.project_id,
            limit=_MAX_RECENT_CALLS,
            descending=True,
        )

        self._render_totals(stats, breakdown)
        self._render_breakdown_table(
            "#llm-by-model-table", breakdown.by_model, totals=breakdown.totals
        )
        self._render_breakdown_table(
            "#llm-by-purpose-table", breakdown.by_purpose, totals=breakdown.totals
        )
        self._render_recent_table(recent)

    def _render_totals(
        self,
        stats: ProjectStats,
        breakdown: LLMActivityBreakdown,
    ) -> None:
        widget = self.query_one("#llm-totals-text", Static)
        if not breakdown.has_calls:
            widget.update(
                "[dim](no LLM calls yet — translate a segment or run intake)[/dim]"
            )
            return
        totals = breakdown.totals
        budget = stats.budget_usd
        budget_line = (
            f" / ${budget:.4f} budget"
            if budget is not None and budget > 0
            else " (no budget set)"
        )
        cache_pct = totals.cache_hit_rate * 100.0
        widget.update(
            "\n".join(
                [
                    f"[b]Calls[/b]: {totals.calls:,}  "
                    f"([b]{totals.cache_hits:,}[/b] cache, "
                    f"{cache_pct:5.1f}%)",
                    f"[b]Tokens[/b]: in {_fmt_int(totals.prompt_tokens)} · "
                    f"out {_fmt_int(totals.completion_tokens)} · "
                    f"total {_fmt_int(totals.total_tokens)}",
                    f"[b]Spend[/b]: ${totals.cost_usd:.4f}{budget_line}",
                ]
            )
        )

    def _render_breakdown_table(
        self,
        table_id: str,
        rows: Sequence[LLMActivityRow],
        *,
        totals: LLMActivityRow,
    ) -> None:
        table = self.query_one(table_id, DataTable)
        table.clear()
        if not rows:
            table.add_row("(no calls)", "—", "—", "—", "—", "—", "—", "—")
            return
        for row in rows:
            share_pct = (
                row.cost_usd / totals.cost_usd * 100.0 if totals.cost_usd else 0.0
            )
            table.add_row(
                row.key,
                f"{row.calls:,}",
                f"{row.cache_hits:,}"
                + (f" ({row.cache_hit_rate * 100:4.1f}%)" if row.calls else ""),
                _fmt_int(row.prompt_tokens),
                _fmt_int(row.completion_tokens),
                _fmt_int(row.total_tokens),
                f"${row.cost_usd:.4f}",
                _share_bar(share_pct),
            )

    def _render_recent_table(self, recent: Sequence[repo.LLMCallRow]) -> None:
        table = self.query_one("#llm-recent-table", DataTable)
        table.clear()
        if not recent:
            table.add_row("(no calls)", "—", "—", "—", "—", "—", "—", "—")
            return
        for row in recent:
            ptok = row.prompt_tokens or 0
            ctok = row.completion_tokens or 0
            cost = f"${row.cost_usd:.4f}" if row.cost_usd is not None else "—"
            seg = row.segment_id[:8] if row.segment_id else "—"
            table.add_row(
                _fmt_ts(row.created_at),
                escape_markup(row.model),
                escape_markup(row.purpose),
                seg,
                _fmt_int(ptok),
                _fmt_int(ctok),
                cost,
                "yes" if row.cache_hit else "no",
            )

    def _set_status(self, message: str) -> None:
        self.query_one("#llm-status", Static).update(message)


def _fmt_int(n: int) -> str:
    """Right-aligned compact int: ``1,234`` / ``12.3K`` / ``1.23M``."""

    if n < 0:
        return f"-{_fmt_int(-n)}"
    if n < 10_000:
        return f"{n:,}"
    if n < 1_000_000:
        return f"{n / 1_000:.1f}K"
    return f"{n / 1_000_000:.2f}M"


def _share_bar(pct: float, *, width: int = 12) -> str:
    """ASCII fraction bar for spend share, kept narrow for table cells."""

    pct = max(0.0, min(100.0, pct))
    filled = max(0, min(width, round(pct / 100.0 * width)))
    return f"{pct:5.1f}% [" + "#" * filled + "·" * (width - filled) + "]"


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "—"
    try:
        return dt.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return str(ts)


__all__ = ["LLMActivityScreen"]
