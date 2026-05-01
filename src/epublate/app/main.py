"""Top-level Textual app for epublate.

M0 boots straight into :class:`ProjectsScreen`. M2 adds the ability to land
on the Reader for a specific project when the app is launched via
``epublate open <project-dir>``. M6 finishes the chrome: a registered
high-contrast theme cycled with ``T``, a global help/cheat-sheet modal
on ``?`` / ``f1``, and persisted theme preferences under
``~/.config/epublate/ui.toml``.

The app also owns a tiny shared :class:`BatchProgress` slot the Dashboard
populates while a batch worker runs. The Reader reads it on its refresh
tick so curators can use the Reader as a live progress monitor while the
batch runs in the background — they see segments flip from
``pending`` → ``translated`` in place and a footer line summarizing the
batch (PRD §4.6 'live updates' / TUI rule §1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from textual.app import App
from textual.binding import Binding, BindingType
from textual.screen import Screen

from epublate.app.config import UIConfig
from epublate.app.screens.help import HelpScreen
from epublate.app.screens.projects import ProjectsScreen
from epublate.app.themes import (
    EPUBLATE_CONTRAST_THEME_NAME,
    EPUBLATE_DEFAULT_THEME_NAME,
    EPUBLATE_THEME_ORDER,
    epublate_contrast_theme,
    epublate_theme,
)
from epublate.core.batch import BatchSummary

_logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BatchProgress:
    """Cross-screen snapshot of an in-flight batch run.

    The Dashboard owns the worker that mutates this; other screens
    (Reader, Inbox) read it to surface progress without coupling to
    the Dashboard's message bus. ``project_id`` is included so a
    Reader on a *different* project ignores progress that doesn't
    belong to it.
    """

    active: bool = False
    project_id: str | None = None
    summary: BatchSummary | None = None
    total: int = 0
    started_at: float | None = None
    paused: bool = False


class EpublateApp(App[None]):
    """Textual application root."""

    TITLE = "epublate"
    SUB_TITLE = "translate ePub story books with an LLM"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("T", "cycle_theme", "Theme", show=True, priority=True),
        Binding("question_mark", "help", "Help", show=True, priority=True),
        Binding("f1", "help", "Help", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        initial_screen: Screen[None] | None = None,
        config_path: Path | None = None,
        ui_config: UIConfig | None = None,
        default_model: str | None = None,
    ) -> None:
        super().__init__()
        self._initial_screen = initial_screen
        self._config_path = config_path
        self._ui_config = ui_config or UIConfig.load(config_path)
        # Translator model the default landing screen should propagate
        # to the Dashboard. Resolved by the CLI from ``$EPUBLATE_LLM_MODEL``
        # (PRD F-LLM-1) so that ``epublate`` and ``epublate open`` agree
        # on which model the user picked.
        self._default_model = default_model
        self.batch_progress: BatchProgress = BatchProgress()

    def on_mount(self) -> None:
        self.register_theme(epublate_theme())
        self.register_theme(epublate_contrast_theme())
        chosen = self._ui_config.theme
        if chosen and chosen in self.available_themes:
            self.theme = chosen
        else:
            # Fresh installs land on the branded look. Cycling with `T`
            # always gets curators back to a stock theme in two keys.
            self.theme = EPUBLATE_DEFAULT_THEME_NAME
        screen: Screen[None] = self._initial_screen or ProjectsScreen(
            default_model=self._default_model,
            ui_config=self._ui_config,
        )
        self.push_screen(screen)

    def action_cycle_theme(self) -> None:
        try:
            idx = EPUBLATE_THEME_ORDER.index(self.theme)
        except ValueError:
            idx = -1
        next_theme = EPUBLATE_THEME_ORDER[(idx + 1) % len(EPUBLATE_THEME_ORDER)]
        self.theme = next_theme
        self._persist_theme(next_theme)

    def action_help(self) -> None:
        target = self.screen
        if isinstance(target, HelpScreen):
            return
        self.push_screen(HelpScreen(target_screen=target))

    def action_toggle_dark(self) -> None:
        # Backwards-compatible shim for the M0 binding (`d`) while the
        # tri-state cycler is the new canonical action; some tests still
        # press ``d`` and expect a flip between the two built-in themes.
        self.theme = "textual-light" if self.theme == "textual-dark" else "textual-dark"
        self._persist_theme(self.theme)

    def _persist_theme(self, name: str) -> None:
        self._ui_config.theme = name
        try:
            self._ui_config.save(self._config_path)
        except OSError as exc:
            _logger.warning("could not persist UI config: %s", exc)


def run(
    *,
    initial_screen: Screen[None] | None = None,
    config_path: Path | None = None,
    default_model: str | None = None,
) -> None:
    """Entry point used by :mod:`epublate.cli`."""

    EpublateApp(
        initial_screen=initial_screen,
        config_path=config_path,
        default_model=default_model,
    ).run()


__all__ = [
    "EPUBLATE_CONTRAST_THEME_NAME",
    "EPUBLATE_DEFAULT_THEME_NAME",
    "BatchProgress",
    "EpublateApp",
    "run",
]
