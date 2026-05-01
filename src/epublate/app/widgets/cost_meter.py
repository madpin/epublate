"""Cost meter widget (PRD §4.6 / F-LLM-7 / F-LLM-8).

Shared between the Dashboard, the Inbox, and the Reader status bar so
the curator sees the running spend wherever they are. The widget is
fully passive — it just renders ``spend_usd`` and ``budget_usd``; the
owning screen is responsible for refreshing those values from
:func:`epublate.core.stats.compute_stats` (or after a batch tick).

We deliberately render plain text rather than a ``ProgressBar`` so
snapshot tests stay stable across Textual versions and the line fits
the docked status row used elsewhere in the TUI.
"""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static


class CostMeter(Static):
    """A single-line widget rendering ``spent / budget (NN%)``.

    ``budget_usd`` may be ``None`` when no cap is set; in that case we
    just display the running spend. The widget reacts to attribute
    assignment so the Dashboard's batch worker can post live updates
    via ``meter.spend_usd = new_value`` without poking the renderer.
    """

    DEFAULT_CSS = """
    CostMeter {
        height: 1;
        padding: 0 1;
    }
    CostMeter.budget-warning {
        color: $warning;
    }
    CostMeter.budget-exceeded {
        color: $error;
    }
    """

    spend_usd: reactive[float] = reactive(0.0)
    budget_usd: reactive[float | None] = reactive[float | None](None)

    def __init__(
        self,
        *,
        spend_usd: float = 0.0,
        budget_usd: float | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__("", id=id, markup=True)
        self.set_reactive(CostMeter.spend_usd, spend_usd)
        self.set_reactive(CostMeter.budget_usd, budget_usd)

    def on_mount(self) -> None:
        self._refresh_meter()

    def watch_spend_usd(self, _old: float, _new: float) -> None:
        self._refresh_meter()

    def watch_budget_usd(self, _old: float | None, _new: float | None) -> None:
        self._refresh_meter()

    def update_values(
        self,
        *,
        spend_usd: float,
        budget_usd: float | None,
    ) -> None:
        """Convenience setter so callers don't need two reactive writes."""

        self.spend_usd = spend_usd
        self.budget_usd = budget_usd

    def _refresh_meter(self) -> None:
        spent = self.spend_usd
        budget = self.budget_usd
        if budget is None or budget <= 0:
            self.update(f"[b]Cost[/b]: ${spent:.4f} (no budget set)")
            self.remove_class("budget-warning")
            self.remove_class("budget-exceeded")
            return
        ratio = spent / budget if budget else 0.0
        pct = ratio * 100.0
        bar = self._bar(ratio)
        self.update(f"[b]Cost[/b]: ${spent:.4f} / ${budget:.4f} ({pct:5.1f}%) {bar}")
        if spent >= budget:
            self.add_class("budget-exceeded")
            self.remove_class("budget-warning")
        elif ratio >= 0.8:
            self.add_class("budget-warning")
            self.remove_class("budget-exceeded")
        else:
            self.remove_class("budget-warning")
            self.remove_class("budget-exceeded")

    @staticmethod
    def _bar(ratio: float, *, width: int = 20) -> str:
        """ASCII progress bar; safe in any terminal and easy to snapshot."""

        filled = max(0, min(width, round(ratio * width)))
        return "[" + "#" * filled + "·" * (width - filled) + "]"


__all__ = ["CostMeter"]
