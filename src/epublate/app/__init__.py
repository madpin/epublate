"""Textual TUI surface for epublate. See ``app.main`` for the entry point."""

from epublate.app.config import UIConfig
from epublate.app.main import EpublateApp
from epublate.app.recents import RecentProject, RecentsStore, record_project

__all__ = [
    "EpublateApp",
    "RecentProject",
    "RecentsStore",
    "UIConfig",
    "record_project",
]
