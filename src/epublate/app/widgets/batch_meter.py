"""Live batch-progress meter (PRD §4.6 / M4).

Renders one line summarizing the in-flight batch: a bar over
``attempted/total``, the four per-status counters, elapsed time, ETA,
and per-segment throughput. Pure: takes a snapshot in
:meth:`BatchProgressMeter.update_snapshot` and re-renders. The owning
screen drives the cadence.

Snapshot rather than live timers so tests stay deterministic and
multiple consumers (Dashboard, Reader status bar) can share one
formatter without each having its own clock.
"""

from __future__ import annotations

from dataclasses import dataclass

from textual.widgets import Static


@dataclass(slots=True, frozen=True)
class BatchSnapshot:
    """Plain payload the meter renders.

    ``total`` is the upfront segment count from ``batch.started``;
    cache hits / failures count toward it so the bar fills to 100% as
    the worker drains the queue. ``elapsed_s`` is the worker's own
    wall clock (the meter never reads ``time.monotonic()`` itself).

    ``chapter_count`` is the number of distinct chapters touched by
    this run, surfaced to the curator as "across N chapters" so they
    have both granularities at a glance (PRD §4.6 / batch UI).

    ``project_total_cost_usd`` is the running project-wide spend —
    the sum of the project's pre-batch spend snapshot and this batch's
    accumulated cost — so curators can compare "this batch" vs
    "the whole project" at a glance.

    ``cancelled`` is set in the final snapshot when the curator
    stopped the run; ``cancelling`` is set on intermediate snapshots
    while the worker is draining in-flight LLM calls after the curator
    pressed cancel.
    """

    attempted: int = 0
    total: int = 0
    chapter_count: int = 0
    translated: int = 0
    cached: int = 0
    flagged: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    project_total_cost_usd: float | None = None
    elapsed_s: float = 0.0
    paused: bool = False
    paused_reason: str | None = None
    cancelled: bool = False
    cancelling: bool = False


class BatchProgressMeter(Static):
    """Multi-line meter for the running batch.

    The rendered block has four sections:

    * Header: "Batch <state>" + the active chapter / segment scope.
    * Bar: ``attempted/total`` plus a 24-cell progress bar.
    * Counters: translated / cached / flagged / failed; cost line
      with both the current batch and the running project total.
    * Wall clock: elapsed, ETA, throughput.

    State badges (``running`` / ``cancelling…`` / ``paused`` /
    ``cancelled`` / ``done``) help the curator see at a glance whether
    a batch is still consuming budget or has wound down.
    """

    DEFAULT_CSS = """
    BatchProgressMeter {
        height: auto;
        padding: 0 1;
    }
    BatchProgressMeter.-active {
        color: $accent;
    }
    BatchProgressMeter.-paused {
        color: $warning;
    }
    BatchProgressMeter.-cancelling {
        color: $warning;
    }
    BatchProgressMeter.-cancelled {
        color: $error;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("[b]Batch[/b]: idle", id=id, markup=True)
        self._snapshot: BatchSnapshot | None = None

    def update_snapshot(self, snapshot: BatchSnapshot | None) -> None:
        self._snapshot = snapshot
        self._refresh()

    def _refresh(self) -> None:
        snap = self._snapshot
        if snap is None or snap.total == 0:
            self.update("[b]Batch[/b]: idle")
            self.remove_class("-active")
            self.remove_class("-paused")
            self.remove_class("-cancelling")
            self.remove_class("-cancelled")
            return
        ratio = snap.attempted / snap.total if snap.total else 0.0
        bar = self._bar(ratio)
        eta = self._eta(snap)
        speed = (
            f"{snap.attempted / snap.elapsed_s:.2f}"
            if snap.elapsed_s > 0 and snap.attempted > 0
            else "—"
        )

        state_badge, css_class = self._state_badge(snap)
        scope = self._scope_line(snap)
        cost_line = self._cost_line(snap)

        header = f"[b]Batch[/b]  {state_badge}{scope}"
        bar_line = (
            f"  {snap.attempted} / {snap.total} segments ({ratio * 100:5.1f}%) {bar}"
        )
        counters = (
            f"  translated {snap.translated} · cached {snap.cached} · "
            f"flagged {snap.flagged} · failed {snap.failed}"
        )
        clock = (
            f"  elapsed {self._fmt_duration(snap.elapsed_s)} · "
            f"ETA {eta} · speed {speed} seg/s"
        )
        if snap.paused and snap.paused_reason:
            clock = f"{clock}\n  [b]paused:[/b] {snap.paused_reason}"

        self.remove_class("-active")
        self.remove_class("-paused")
        self.remove_class("-cancelling")
        self.remove_class("-cancelled")
        if css_class:
            self.add_class(css_class)
        self.update("\n".join([header, bar_line, counters, cost_line, clock]))

    @staticmethod
    def _state_badge(snap: BatchSnapshot) -> tuple[str, str]:
        """Pick the human-readable state label + CSS class for the meter."""

        if snap.cancelled:
            return "[b][error]cancelled[/error][/b]", "-cancelled"
        if snap.cancelling:
            return "[b][warning]cancelling…[/warning][/b]", "-cancelling"
        if snap.paused:
            return "[b][warning]paused[/warning][/b]", "-paused"
        if snap.attempted >= snap.total:
            return "[b]done[/b]", ""
        return "[b]running[/b]", "-active"

    @staticmethod
    def _scope_line(snap: BatchSnapshot) -> str:
        """Format "across N chapters · M segments" or "" if no chapters."""

        if snap.chapter_count <= 0:
            return f"  ·  [b]{snap.total}[/b] segments queued"
        chapter_word = "chapter" if snap.chapter_count == 1 else "chapters"
        return (
            f"  ·  across [b]{snap.chapter_count}[/b] {chapter_word} · "
            f"[b]{snap.total}[/b] segments queued"
        )

    @staticmethod
    def _cost_line(snap: BatchSnapshot) -> str:
        """Render "this batch: $X · project total: $Y" (or just batch)."""

        batch_str = f"this batch ${snap.cost_usd:.4f}"
        if snap.project_total_cost_usd is None:
            return f"  {batch_str}"
        return f"  {batch_str}  ·  project total ${snap.project_total_cost_usd:.4f}"

    @staticmethod
    def _eta(snap: BatchSnapshot) -> str:
        """Project elapsed time to the remaining queue."""

        if snap.attempted == 0 or snap.elapsed_s <= 0:
            return "—"
        if snap.attempted >= snap.total:
            return "0:00"
        per_seg = snap.elapsed_s / snap.attempted
        remaining = max(0, snap.total - snap.attempted)
        return BatchProgressMeter._fmt_duration(remaining * per_seg)

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        s = round(seconds)
        h, rem = divmod(s, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"

    @staticmethod
    def _bar(ratio: float, *, width: int = 24) -> str:
        ratio = max(0.0, min(1.0, ratio))
        filled = round(ratio * width)
        return "[" + "#" * filled + "·" * (width - filled) + "]"


__all__ = ["BatchProgressMeter", "BatchSnapshot"]
