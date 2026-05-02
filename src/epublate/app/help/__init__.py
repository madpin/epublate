"""Markdown content shipped with the in-app Help modal (PRD §4.6 / M6).

Each ``*.md`` file is loaded by :class:`epublate.app.screens.help.HelpScreen`
into a :class:`textual.widgets.MarkdownViewer` tab pane. Adding a new
section is a one-file change; no Python edits required.
"""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

HELP_PACKAGE = "epublate.app.help"


def help_text(slug: str) -> str:
    """Return the contents of ``<slug>.md`` shipped with the package.

    Raises :class:`FileNotFoundError` if the slug doesn't exist; the
    Help screen catches and renders a placeholder so a missing file
    never crashes the modal.
    """

    resource = files(HELP_PACKAGE) / f"{slug}.md"
    return resource.read_text(encoding="utf-8")


def help_path(slug: str) -> Path:
    """Return the on-disk path to a help file for snapshot/debug use."""

    return Path(str(files(HELP_PACKAGE) / f"{slug}.md"))


__all__ = ["HELP_PACKAGE", "help_path", "help_text"]
