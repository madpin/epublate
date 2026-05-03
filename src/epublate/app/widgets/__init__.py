"""Reusable Textual widgets shared across screens.

The cost meter and the batch-progress meter (PRD §4.6) are the
shared widgets that render run-time stats from the Dashboard worker
in a couple of different places (the Dashboard itself, the Reader
status bar, future logs screen).

The :class:`BatchStatusBar` is the slim variant of the batch meter
that stays mounted on every "main" screen (Glossary, Inbox,
Settings, …) so the curator never loses sight of an in-flight batch
even while they're triaging a different surface.
"""

from epublate.app.widgets.batch_meter import BatchProgressMeter, BatchSnapshot
from epublate.app.widgets.batch_status_bar import BatchStatusBar
from epublate.app.widgets.cost_meter import CostMeter

__all__ = [
    "BatchProgressMeter",
    "BatchSnapshot",
    "BatchStatusBar",
    "CostMeter",
]
