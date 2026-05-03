"""Cross-screen Textual messages emitted by the App-level batch worker.

These are intentionally defined outside of :mod:`epublate.app.main` so
both the App (which posts them) and the Dashboard (which handles them
via ``@on(...)`` decorators that run at class-definition time) can
import them without triggering the
``epublate.app -> main -> screens -> dashboard -> main`` import cycle.

The Dashboard re-handles ``BatchTick`` / ``BatchFinished`` to keep its
panel in sync; other screens (Reader, Inbox) read the
:class:`epublate.app.main.BatchProgress` slot on their own refresh tick
instead of subscribing to the messages.
"""

from __future__ import annotations

from textual.message import Message

from epublate.core.batch import BatchPrePassProgress, BatchProgressEvent, BatchSummary


class BatchTick(Message):
    """One per-segment progress tick, posted from the App's batch worker."""

    def __init__(self, event: BatchProgressEvent) -> None:
        super().__init__()
        self.event = event


class BatchPrePassTick(Message):
    """One per-chunk pre-pass progress tick.

    The dashboard's status line uses these to show
    "pre-pass: ch 1/3 chunk 2/2" while the helper LLM is grinding —
    without this signal a slow helper makes the meter sit at
    ``0 / N`` for minutes and looks like a deadlock. The Reader /
    Inbox don't subscribe; they read the audit-log events instead.
    """

    def __init__(self, event: BatchPrePassProgress) -> None:
        super().__init__()
        self.event = event


class BatchFinished(Message):
    """The App's batch worker terminated.

    ``status`` distinguishes the four end-states the runner can
    report: ``"completed"`` (all segments attempted), ``"paused"``
    (budget cap), ``"cancelled"`` (curator action), ``"failed"``
    (uncaught exception). Listeners (Dashboard) inspect ``status`` to
    decide which message to surface.
    """

    def __init__(
        self,
        *,
        status: str,
        summary: BatchSummary | None,
        error: str | None = None,
        project_id: str,
    ) -> None:
        super().__init__()
        self.status = status
        self.summary = summary
        self.error = error
        self.project_id = project_id


__all__ = ["BatchFinished", "BatchPrePassTick", "BatchTick"]
