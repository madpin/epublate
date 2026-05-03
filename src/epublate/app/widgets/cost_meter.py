"""Cost meter widget (PRD §4.6 / F-LLM-7 / F-LLM-8).

Shared between the Dashboard, the Inbox, and the Reader status bar so
the curator sees the running spend wherever they are. The widget is
fully passive — it just renders ``spend_usd``, ``budget_usd``, and
optionally a prompt/completion token breakdown; the owning screen is
responsible for refreshing those values from
:func:`epublate.core.stats.compute_stats` (or after a batch tick).

We deliberately render plain text rather than a ``ProgressBar`` so
snapshot tests stay stable across Textual versions and the lines fit
the docked status row used elsewhere in the TUI.
"""

from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static


class CostMeter(Static):
    """A multi-line widget rendering ``spent / budget`` plus token totals.

    ``budget_usd`` may be ``None`` when no cap is set; in that case we
    just display the running spend. ``prompt_tokens`` and
    ``completion_tokens`` are optional — when both are zero (no LLM
    calls yet) the second line is suppressed so the meter stays
    one-line for the docked status bar; otherwise the breakdown
    surfaces input/output tokens so the curator can spot a runaway
    output without diving into the LLM activity panel
    (PRD §4.6 / F-LLM-7).

    The widget reacts to attribute assignment so the Dashboard's batch
    worker can post live updates via ``meter.spend_usd = new_value``
    without poking the renderer.
    """

    DEFAULT_CSS = """
    CostMeter {
        height: auto;
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
    prompt_tokens: reactive[int] = reactive(0)
    completion_tokens: reactive[int] = reactive(0)
    llm_calls: reactive[int] = reactive(0)
    cache_hits: reactive[int] = reactive(0)

    def __init__(
        self,
        *,
        spend_usd: float = 0.0,
        budget_usd: float | None = None,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        llm_calls: int = 0,
        cache_hits: int = 0,
        id: str | None = None,
    ) -> None:
        super().__init__("", id=id, markup=True)
        self.set_reactive(CostMeter.spend_usd, spend_usd)
        self.set_reactive(CostMeter.budget_usd, budget_usd)
        self.set_reactive(CostMeter.prompt_tokens, prompt_tokens)
        self.set_reactive(CostMeter.completion_tokens, completion_tokens)
        self.set_reactive(CostMeter.llm_calls, llm_calls)
        self.set_reactive(CostMeter.cache_hits, cache_hits)

    def on_mount(self) -> None:
        self._refresh_meter()

    def watch_spend_usd(self, _old: float, _new: float) -> None:
        self._refresh_meter()

    def watch_budget_usd(self, _old: float | None, _new: float | None) -> None:
        self._refresh_meter()

    def watch_prompt_tokens(self, _old: int, _new: int) -> None:
        self._refresh_meter()

    def watch_completion_tokens(self, _old: int, _new: int) -> None:
        self._refresh_meter()

    def watch_llm_calls(self, _old: int, _new: int) -> None:
        self._refresh_meter()

    def watch_cache_hits(self, _old: int, _new: int) -> None:
        self._refresh_meter()

    def update_values(
        self,
        *,
        spend_usd: float,
        budget_usd: float | None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        llm_calls: int | None = None,
        cache_hits: int | None = None,
    ) -> None:
        """Convenience setter so callers don't need many reactive writes."""

        self.spend_usd = spend_usd
        self.budget_usd = budget_usd
        if prompt_tokens is not None:
            self.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            self.completion_tokens = completion_tokens
        if llm_calls is not None:
            self.llm_calls = llm_calls
        if cache_hits is not None:
            self.cache_hits = cache_hits

    def _refresh_meter(self) -> None:
        spent = self.spend_usd
        budget = self.budget_usd
        if budget is None or budget <= 0:
            cost_line = f"[b]Cost[/b]: ${spent:.4f} [dim](no budget set)[/dim]"
            self.remove_class("budget-warning")
            self.remove_class("budget-exceeded")
        else:
            ratio = spent / budget if budget else 0.0
            pct = ratio * 100.0
            bar = self._bar(ratio)
            cost_line = f"[b]Cost[/b]: ${spent:.4f} / ${budget:.4f} ({pct:5.1f}%) {bar}"
            if spent >= budget:
                self.add_class("budget-exceeded")
                self.remove_class("budget-warning")
            elif ratio >= 0.8:
                self.add_class("budget-warning")
                self.remove_class("budget-exceeded")
            else:
                self.remove_class("budget-warning")
                self.remove_class("budget-exceeded")

        prompt = self.prompt_tokens
        completion = self.completion_tokens
        token_line: str | None
        if prompt or completion:
            token_line = (
                f"[b]Tokens[/b]: in {self._fmt_tokens(prompt)} · "
                f"out {self._fmt_tokens(completion)} · "
                f"total {self._fmt_tokens(prompt + completion)}"
            )
        else:
            token_line = None

        calls = self.llm_calls
        hits = self.cache_hits
        calls_line: str | None
        if calls:
            hit_pct = (hits / calls * 100.0) if calls else 0.0
            calls_line = (
                f"[b]Calls[/b]: {calls} ({hits} cache hit"
                f"{'s' if hits != 1 else ''}, {hit_pct:.1f}%)"
            )
        else:
            calls_line = None

        lines = [cost_line]
        if token_line is not None:
            lines.append(token_line)
        if calls_line is not None:
            lines.append(calls_line)
        self.update("\n".join(lines))

    @staticmethod
    def _bar(ratio: float, *, width: int = 20) -> str:
        """ASCII progress bar; safe in any terminal and easy to snapshot."""

        filled = max(0, min(width, round(ratio * width)))
        return "[" + "#" * filled + "·" * (width - filled) + "]"

    @staticmethod
    def _fmt_tokens(n: int) -> str:
        """Compact token count: ``1234`` → ``1,234``; ``1_500_000`` → ``1.5M``.

        Big books generate millions of tokens fast; the dashboard line
        shouldn't have to fight for horizontal space with raw integers.
        """

        if n < 0:
            return f"-{CostMeter._fmt_tokens(-n)}"
        if n < 10_000:
            return f"{n:,}"
        if n < 1_000_000:
            return f"{n / 1_000:.1f}K"
        return f"{n / 1_000_000:.2f}M"


__all__ = ["CostMeter"]
