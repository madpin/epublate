"""Textual screens (PRD §4.6).

Boot order: ``ProjectsScreen`` (M0) → ``DashboardScreen`` (M4) →
``ReaderScreen`` / ``GlossaryScreen`` / ``InboxScreen`` (M2/M3/M4) →
``SettingsScreen`` / ``HelpScreen`` (M6).
"""

from epublate.app.screens.dashboard import DashboardScreen
from epublate.app.screens.glossary import GlossaryScreen
from epublate.app.screens.help import HelpScreen
from epublate.app.screens.inbox import InboxScreen
from epublate.app.screens.new_project import NewProjectModal
from epublate.app.screens.open_project import OpenProjectModal
from epublate.app.screens.projects import ProjectsScreen
from epublate.app.screens.reader import ReaderScreen
from epublate.app.screens.settings import SettingsScreen

__all__ = [
    "DashboardScreen",
    "GlossaryScreen",
    "HelpScreen",
    "InboxScreen",
    "NewProjectModal",
    "OpenProjectModal",
    "ProjectsScreen",
    "ReaderScreen",
    "SettingsScreen",
]
