"""In-TUI open-project modal (PRD §4.6 / §7.1).

Curators can open an existing project either by selecting it from the
recents table on the Projects screen, or by typing / browsing to its
path here. The modal supports two entry modes:

* **Type a path** into the input and press Enter.
* **Browse** with the built-in file browser (Ctrl+O) which offers
  quick-access buttons for the projects root, Documents, Downloads,
  etc., and lets curators descend into the folder they want.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, Rule, Static

from epublate.app.branding import ICON_ARROW
from epublate.app.paths import default_projects_root
from epublate.app.recents import record_project
from epublate.app.screens.file_browser import FileBrowserModal
from epublate.core.project import Project
from epublate.errors import ConfigurationError

_logger = logging.getLogger(__name__)


class OpenProjectModal(ModalScreen[Project | None]):
    """Modal for the ``open project`` flow."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+s", "submit", "Open", show=True),
        Binding("ctrl+o", "browse", "Browse", show=True),
    ]

    DEFAULT_CSS = """
    OpenProjectModal {
        align: center middle;
        background: $background 60%;
    }
    OpenProjectModal #open-project-box {
        width: 80%;
        max-width: 100;
        height: auto;
        border: round $accent;
        padding: 1 2;
        background: $panel;
    }
    OpenProjectModal #open-project-title {
        text-style: bold;
        color: $primary;
        padding: 0;
    }
    OpenProjectModal #open-project-subtitle {
        color: $text-muted;
        padding: 0 0 1 0;
        height: 1;
    }
    OpenProjectModal Rule {
        color: $accent 50%;
        margin: 0 0 1 0;
    }
    OpenProjectModal .field-row {
        height: auto;
        padding: 0;
        margin: 0 0 1 0;
    }
    OpenProjectModal .field-label {
        width: 14;
        padding: 1 1 0 0;
        color: $secondary;
        text-style: bold;
    }
    OpenProjectModal .field-stack {
        width: 1fr;
        height: auto;
    }
    OpenProjectModal .field-input-row {
        height: auto;
    }
    OpenProjectModal .field-input-row Input {
        width: 1fr;
    }
    OpenProjectModal .field-input-row Button {
        width: 12;
        height: 3;
        margin: 0 0 0 1;
    }
    OpenProjectModal .field-hint {
        color: $text-muted;
        padding: 0 0 0 1;
        height: auto;
    }
    OpenProjectModal Input {
        background: $surface;
    }
    OpenProjectModal Input:focus {
        border: tall $accent;
    }
    OpenProjectModal #open-project-error {
        color: $error;
        text-style: bold;
        padding: 1 0 0 0;
        height: auto;
    }
    OpenProjectModal #open-project-buttons {
        height: 3;
        padding: 1 0 0 0;
        align-horizontal: right;
    }
    OpenProjectModal #open-project-buttons Button {
        margin: 0 0 0 1;
    }
    OpenProjectModal #open-project-hint {
        color: $text-muted;
        padding: 1 0 0 0;
        height: auto;
    }
    """

    def __init__(
        self,
        *,
        default_path: Path | None = None,
        recents_path: Path | None = None,
        projects_root: Path | None = None,
    ) -> None:
        super().__init__()
        self._default_path = default_path
        self._recents_path = recents_path
        self._projects_root = (
            projects_root.expanduser().resolve()
            if projects_root is not None
            else default_projects_root()
        )

    def compose(self) -> ComposeResult:
        with Vertical(id="open-project-box"):
            yield Label(f"  {ICON_ARROW} Open project", id="open-project-title")
            yield Label(
                "Re-attach to an existing project folder by path.",
                id="open-project-subtitle",
            )
            yield Rule(line_style="thick")
            with Horizontal(classes="field-row"):
                yield Label("Project dir", classes="field-label")
                with Vertical(classes="field-stack"):
                    with Horizontal(classes="field-input-row"):
                        yield Input(
                            value=str(self._default_path) if self._default_path else "",
                            placeholder="/path/to/project-dir",
                            id="open-project-path",
                        )
                        yield Button(
                            "Browse…", id="open-project-browse", variant="default"
                        )
                    yield Static(
                        "must contain original.epub and a *.epublate database — "
                        "press [b]Ctrl+O[/b] (or [b]Browse[/b]) to pick a folder",
                        classes="field-hint",
                        markup=True,
                    )
            yield Static("", id="open-project-error", markup=False)
            yield Static(
                "[dim]Submit with [/][b]Ctrl+S[/]"
                "[dim] or [/][b]Enter[/]"
                "[dim]; cancel with [/][b]Esc[/]"
                "[dim]; browse with [/][b]Ctrl+O[/][dim].[/]",
                id="open-project-hint",
                markup=True,
            )
            with Horizontal(id="open-project-buttons"):
                yield Button("Cancel", id="open-project-cancel", variant="default")
                yield Button(
                    "Open project",
                    id="open-project-submit",
                    variant="primary",
                )
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#open-project-path", Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "open-project-submit":
            self.action_submit()
            return
        if button_id == "open-project-cancel":
            self.action_cancel()
            return
        if button_id == "open-project-browse":
            self.action_browse()
            return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        del event
        self.action_submit()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_browse(self) -> None:
        raw = self.query_one("#open-project-path", Input).value.strip()
        start: Path | None = None
        if raw:
            candidate = Path(raw).expanduser()
            if candidate.is_dir():
                start = candidate
            elif candidate.parent.is_dir():
                start = candidate.parent
        if start is None:
            start = self._projects_root if self._projects_root.is_dir() else None
        modal = FileBrowserModal(
            mode="directory",
            start_path=start,
            title=f"  {ICON_ARROW} Pick project folder",
            subtitle=(
                "Navigate to the folder that holds original.epub + a "
                "*.epublate database, then press 'Pick this folder'."
            ),
        )
        self.app.push_screen(modal, self._on_folder_picked)

    def _on_folder_picked(self, result: Path | None) -> None:
        if result is None:
            return
        self.query_one("#open-project-path", Input).value = str(result)

    def action_submit(self) -> None:
        try:
            project = self._open_from_form()
        except _FormError as exc:
            self._set_error(str(exc))
            return
        except ConfigurationError as exc:
            self._set_error(str(exc))
            return
        except Exception as exc:
            _logger.exception("unexpected error opening project")
            self._set_error(f"unexpected error: {exc}")
            return
        record_project(
            project_dir=project.project_dir,
            name=project.name,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            path=self._recents_path,
        )
        self.dismiss(project)

    def _open_from_form(self) -> Project:
        raw = self.query_one("#open-project-path", Input).value.strip()
        if not raw:
            raise _FormError("Project directory is required.")
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        else:
            path = path.resolve()
        if not path.is_dir():
            raise _FormError(f"Not a directory: {path}")
        return Project.open(path)

    def _set_error(self, message: str) -> None:
        self.query_one("#open-project-error", Static).update(message)


class _FormError(Exception):
    """Raised when a form field fails validation."""


__all__ = ["OpenProjectModal"]
