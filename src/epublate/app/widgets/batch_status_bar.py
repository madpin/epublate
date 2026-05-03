"""Compact persistent banner for the App-level batch worker.

Mounted on every screen except the Dashboard (which has its full
``BatchProgressMeter`` panel) and the Reader (which has a multi-line
footer mirror). The bar reads
:attr:`epublate.app.main.EpublateApp.batch_progress` on a 500 ms timer
so it tracks the worker as it runs and self-hides as soon as the
batch terminates. This is what keeps the curator anchored when they
navigate to the Inbox / Glossary / Settings while a long batch runs:
they always know whether the worker is still consuming budget without
having to step back to the Dashboard.

Single-line layout (with a wrap-around line for the cost details when
the bar is too narrow), color-coded by state:

* ``running``    — accent
* ``cancelling`` — warning
* ``paused``     — warning
* ``cancelled``  — error
* ``done``       — neutral

The bar is also fully ``aria``-friendly: it sets ``ARIA_LIVE`` so
screen readers see status updates without yanking focus
(PRD NFR-9 / accessibility).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widgets import Static

if TYPE_CHECKING:
    from epublate.app.main import BatchProgress


_REFRESH_SECONDS = 0.5


def _state_label(progress: BatchProgress) -> tuple[str, str]:
    """Pick a human-readable state label + CSS class for the bar."""

    if progress.cancelling:
        return ("cancelling…", "-cancelling")
    if progress.paused:
        return ("paused", "-paused")
    return ("running", "-active")


def _format_compact(progress: BatchProgress) -> str:
    """Render the running batch in a single Rich-markup line.

    Surfaces the four counters the curator needs at a glance — state,
    progress, chapter scope, and cost — in a compact form that fits on
    most terminals (≈ 100 cols). The cost line shows both this batch
    and the running project total so curators can compare the two.
    """

    summary = progress.summary
    label, _css = _state_label(progress)
    if summary is None:
        attempted = 0
        translated = 0
        cached = 0
        flagged = 0
        failed = 0
        cost_usd = 0.0
    else:
        attempted = summary.attempted
        translated = summary.translated
        cached = summary.cached
        flagged = summary.flagged
        failed = summary.failed
        cost_usd = summary.cost_usd
    total = max(progress.total, attempted)
    pct = (attempted / total * 100.0) if total else 0.0
    chapter_word = "chapter" if progress.chapter_count == 1 else "chapters"
    if progress.chapter_count > 0:
        chapter_strip = (
            f"  ·  {progress.chapter_count} {chapter_word} ·  "
            f"{translated}✓ {cached}≡ {flagged}! {failed}✗"
        )
    else:
        chapter_strip = f"  ·  {translated}✓ {cached}≡ {flagged}! {failed}✗"
    cost_strip = f"  ·  this batch ${cost_usd:.4f}"
    starting_spend = progress.starting_spend_usd
    if starting_spend or summary is not None:
        cost_strip = (
            f"{cost_strip}  ·  project total ${(starting_spend + cost_usd):.4f}"
        )
    return (
        f"[b]{label}[/b]  ·  {attempted}/{total} ({pct:5.1f}%)"
        f"{chapter_strip}{cost_strip}"
    )


class BatchStatusBar(Static):
    """Slim bottom-docked banner mirroring the App's batch state."""

    DEFAULT_CSS = """
    BatchStatusBar {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $boost;
        color: $text-muted;
        display: none;
        text-style: bold;
    }
    BatchStatusBar.-active {
        display: block;
        background: $accent 20%;
        color: $accent;
    }
    BatchStatusBar.-cancelling {
        display: block;
        background: $warning 20%;
        color: $warning;
    }
    BatchStatusBar.-paused {
        display: block;
        background: $warning 20%;
        color: $warning;
    }
    """

    ARIA_LIVE = "polite"

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__("", id=id, markup=True)

    def on_mount(self) -> None:
        # Run one synchronous refresh so the banner shows immediately
        # if the curator navigates into a screen while a batch is
        # already in flight (no flicker waiting for the first tick).
        self._refresh_now()
        self.set_interval(_REFRESH_SECONDS, self._refresh_now)

    def _refresh_now(self) -> None:
        # Lazy-imported because ``epublate.app.main`` itself imports
        # the screens that mount this widget; a top-level import would
        # close the cycle.
        from epublate.app.main import BatchProgress, EpublateApp

        app = self.app
        if not isinstance(app, EpublateApp):
            self._hide()
            return
        progress = app.batch_progress
        if not isinstance(progress, BatchProgress) or not progress.active:
            self._hide()
            return
        _label, css_class = _state_label(progress)
        self.remove_class("-active")
        self.remove_class("-cancelling")
        self.remove_class("-paused")
        self.add_class(css_class)
        self.update(_format_compact(progress))

    def _hide(self) -> None:
        if (
            self.has_class("-active")
            or self.has_class("-cancelling")
            or self.has_class("-paused")
        ):
            self.remove_class("-active")
            self.remove_class("-cancelling")
            self.remove_class("-paused")
        # Empty content keeps the line invisible when ``display: none``
        # is in effect; we still clear it so the next time the bar
        # becomes visible the previous tick's text doesn't briefly
        # flash through.
        self.update("")


__all__ = ["BatchStatusBar"]
