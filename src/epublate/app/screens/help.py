"""Help modal — tabbed cheat sheet + concept guide (PRD §4.6 / M6).

The modal opens from any screen via the global ``?`` / F1 binding. We
introspect the *currently active* screen's ``BINDINGS`` for the
**Keys** tab so the cheat sheet stays in sync as bindings evolve;
the **Concepts**, **Workflows**, and **Troubleshooting** tabs are
backed by Markdown files in :mod:`epublate.app.help` so contributors
can extend the documentation without touching Python.

Markdown is rendered via :class:`textual.widgets.Markdown`. We
intentionally avoid :class:`MarkdownViewer` (which adds a TOC sidebar)
because the modal is already constrained to ~80% of the terminal and
the in-pane TOC eats too much horizontal space.
"""

from __future__ import annotations

import logging
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Label, Markdown, Static, TabbedContent, TabPane

from epublate.app.help import help_text
from epublate.app.themes import EPUBLATE_THEME_ORDER

_logger = logging.getLogger(__name__)

GLOBAL_HELP_HINT = "[b]Global[/b]: ? / F1 = this help, T = cycle theme, Ctrl+C = quit."


class HelpScreen(ModalScreen[None]):
    """Per-screen keybinding cheat sheet + concept reference (PRD §4.6)."""

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
        width: 90%;
        height: 90%;
        max-width: 120;
        border: round $primary;
        padding: 1 2;
        background: $surface;
    }
    HelpScreen #help-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    HelpScreen TabbedContent {
        height: 1fr;
    }
    HelpScreen TabPane {
        padding: 1 1;
    }
    HelpScreen #help-bindings {
        height: 1fr;
        padding: 0 0 1 0;
    }
    HelpScreen #help-body {
        height: auto;
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
    HelpScreen .help-md {
        height: 1fr;
    }
    """

    TAB_SLUGS: ClassVar[tuple[tuple[str, str, str], ...]] = (
        # (tab id, label, markdown slug)
        ("help-tab-concepts", "Concepts", "concepts"),
        ("help-tab-workflows", "Workflows", "workflows"),
        ("help-tab-troubleshoot", "Troubleshooting", "troubleshooting"),
        ("help-tab-keys-md", "Keys (reference)", "keys"),
    )

    def __init__(self, *, target_screen: Screen[object] | None = None) -> None:
        super().__init__()
        self._target_screen = target_screen

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Label(self._title_text(), id="help-title")
            with TabbedContent(id="help-tabs", initial="help-tab-keys"):
                with (
                    TabPane("Keys (this screen)", id="help-tab-keys"),
                    VerticalScroll(id="help-bindings"),
                ):
                    yield Static(
                        self._bindings_text(),
                        id="help-body",
                        markup=True,
                    )
                for tab_id, label, slug in self.TAB_SLUGS:
                    with TabPane(label, id=tab_id):
                        try:
                            md = help_text(slug)
                        except FileNotFoundError:
                            _logger.warning("missing help markdown: %s.md", slug)
                            md = (
                                f"# Help section missing\n\n"
                                f"Could not load `{slug}.md` from the help "
                                "package. Please file an issue.\n"
                            )
                        with VerticalScroll(classes="help-md"):
                            yield Markdown(md, id=f"help-md-{slug}")
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
