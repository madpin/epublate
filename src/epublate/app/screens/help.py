"""Help / cheat-sheet modal (PRD §4.6 / M6).

The :class:`HelpScreen` introspects the *currently active* screen's
``BINDINGS`` and renders them as a one-page cheat sheet. We deliberately
introspect at render time (instead of hardcoding a static table) so the
help stays in sync as bindings evolve in later milestones.

Bound globally on :class:`epublate.app.main.EpublateApp` to ``?`` and
``f1``; pressing either key opens this modal regardless of which screen
the curator is on.
"""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Label, Static

from epublate.app.themes import EPUBLATE_THEME_ORDER

GLOBAL_HELP_HINT = "[b]Global[/b]: ? / F1 = this help, T = cycle theme, Ctrl+C = quit."


class HelpScreen(ModalScreen[None]):
    """Per-screen keybinding cheat sheet (PRD §4.6)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "dismiss", "Close", show=True),
        Binding("q", "dismiss", "Close", show=False),
        Binding("?", "dismiss", "Close", show=False),
    ]

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    HelpScreen #help-box {
        width: 80%;
        height: 80%;
        max-width: 100;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    HelpScreen #help-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    HelpScreen #help-bindings {
        height: 1fr;
        padding: 0 0 1 0;
    }
    HelpScreen #help-global {
        height: auto;
        color: $text-muted;
        padding: 1 0 0 0;
    }
    HelpScreen #help-themes {
        height: auto;
        color: $text-muted;
    }
    """

    def __init__(self, *, target_screen: Screen[object] | None = None) -> None:
        super().__init__()
        self._target_screen = target_screen

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Label(self._title_text(), id="help-title")
            with VerticalScroll(id="help-bindings"):
                yield Static(self._bindings_text(), id="help-body", markup=True)
            yield Static(GLOBAL_HELP_HINT, id="help-global", markup=True)
            yield Static(self._themes_text(), id="help-themes", markup=True)
        yield Footer()

    def action_dismiss(self, _result: None = None) -> None:  # type: ignore[override]
        self.dismiss(None)

    def _title_text(self) -> str:
        screen = self._target_screen
        name = screen.__class__.__name__ if screen is not None else "epublate"
        return f"Help — {name}"

    def _bindings_text(self) -> str:
        screen = self._target_screen
        if screen is None:
            return "(no screen-specific bindings)"
        rows = _collect_bindings(screen)
        if not rows:
            return "(this screen has no key bindings)"
        width = max(len(key) for key, _action, _desc in rows)
        lines = [f"  [b]{key.ljust(width)}[/b]   {desc}" for key, _action, desc in rows]
        return "\n".join(lines)

    def _themes_text(self) -> str:
        names = ", ".join(EPUBLATE_THEME_ORDER)
        return f"[b]Themes[/b] (cycle with [b]T[/b]): {names}"


def _collect_bindings(screen: Screen[object]) -> list[tuple[str, str, str]]:
    """Flatten a screen's ``BINDINGS`` to ``(key, action, description)``.

    Hidden bindings (``show=False``) are still surfaced — the cheat
    sheet's whole point is to make every action discoverable, even the
    ones we hide from the footer to keep that line short.
    """

    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw in getattr(screen, "BINDINGS", ()):
        binding = _coerce_binding(raw)
        if binding is None:
            continue
        key = binding.key
        action = binding.action
        desc = binding.description or action
        sig = (key, action)
        if sig in seen:
            continue
        seen.add(sig)
        out.append((key, action, desc))
    out.sort(key=lambda r: r[0])
    return out


def _coerce_binding(raw: object) -> Binding | None:
    if isinstance(raw, Binding):
        return raw
    if isinstance(raw, tuple) and len(raw) >= 2:
        try:
            return Binding(*raw)
        except TypeError:
            return None
    return None


__all__ = ["HelpScreen"]
