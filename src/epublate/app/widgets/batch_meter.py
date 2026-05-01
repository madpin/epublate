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
    """

    attempted: int = 0
    total: int = 0
    translated: int = 0
    cached: int = 0
    flagged: int = 0
    failed: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    paused: bool = False
    paused_reason: str | None = None


class BatchProgressMeter(Static):
    """One-line meter for the running batch.

    The rendered line has three sections:

    * ``attempted/total`` plus a 24-cell bar.
    * Per-status counters (translated / cached / flagged / failed) and
      cumulative cost.
    * Elapsed wall clock, ETA, and throughput in segments-per-second.

    When idle the widget renders an explicit "Idle" so curators always
    know whether a batch is currently running.
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
            return
        ratio = snap.attempted / snap.total if snap.total else 0.0
        bar = self._bar(ratio)
        eta = self._eta(snap)
        speed = (
            f"{snap.attempted / snap.elapsed_s:.2f}"
            if snap.elapsed_s > 0 and snap.attempted > 0
            else "—"
        )
        line1 = (
            f"[b]Batch[/b]: {snap.attempted}/{snap.total} ({ratio * 100:5.1f}%) {bar}"
        )
        line2 = (
            f"  translated {snap.translated}, cached {snap.cached}, "
            f"flagged {snap.flagged}, failed {snap.failed} — "
            f"cost ${snap.cost_usd:.4f}"
        )
        line3 = (
            f"  elapsed {self._fmt_duration(snap.elapsed_s)} — "
            f"ETA {eta} — speed {speed} seg/s"
        )
        if snap.paused:
            line1 = f"{line1}  [b]paused[/b]"
            if snap.paused_reason:
                line3 = f"{line3}\n  [b]paused:[/b] {snap.paused_reason}"
            self.add_class("-paused")
            self.remove_class("-active")
        else:
            self.add_class("-active")
            self.remove_class("-paused")
        self.update(f"{line1}\n{line2}\n{line3}")

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
