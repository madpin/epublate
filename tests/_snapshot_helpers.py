"""Helpers shared by ``tests/test_snapshot_*.py`` (PRD §4.6 / M6).

Snapshot tests are byte-compared SVGs. Anything that's *not* deterministic
across runs/machines (absolute paths, real timestamps, host-local UUIDs)
has to be masked **after** the screen mounts and *before* the screenshot
fires. We do that via a small ``run_before`` callable per test.

Keeping the masks here means each new snapshot test is two lines: import
``mask_dynamic_text`` and pass it to ``snap_compare(run_before=...)``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

# pytest_textual_snapshot accepts a ``Pilot`` here but we don't want to
# import Textual's private types into test fixtures.
PilotLike = Any


def mask_dashboard_project(pilot: PilotLike) -> Callable[[], Awaitable[None]] | None:
    """Replace the path-bearing project block on the Dashboard.

    The Dashboard renders the project's absolute path, which lives under
    ``tmp_path`` and changes between every pytest run. We swap it for a
    stable placeholder so the snapshot is portable.
    """

    async def _run() -> None:
        await pilot.pause()
        screen = pilot.app.screen
        for widget_id, replacement in (
            (
                "dashboard-project",
                "[b]snapshot-proj[/b]  (en → pt)\n  source: <project>/original.epub",
            ),
        ):
            try:
                widget = screen.query_one(f"#{widget_id}")
            except Exception:
                continue
            widget.update(replacement)
        await pilot.pause()

    return _run()


def mask_settings_paths(pilot: PilotLike) -> Callable[[], Awaitable[None]] | None:
    """Replace path-bearing rows on the Settings screen with placeholders."""

    async def _run() -> None:
        await pilot.pause()
        screen = pilot.app.screen
        replacements = {
            "settings-project-body": (
                "  name        : snapshot-proj\n"
                "  source lang : en\n"
                "  target lang : pt\n"
                "  source epub : <project>/original.epub\n"
                "  database    : <project>/snapshot.epublate\n"
                "  budget cap  : (none)"
            ),
            "settings-style-body": (
                "  preset  : Literary fiction\n"
                "  preview : Translate as adult literary fiction. "
                "Preserve narrator's voice, rhythm, and subtext.\n"
                "  size    : 600 chars in the system prompt"
            ),
            "settings-ui-body": (
                "  active theme    : textual-dark\n"
                "  saved theme     : textual-dark\n"
                "  config file     : <home>/.config/epublate/ui.toml\n"
                "  cycle order     : textual-dark, textual-light, epublate-contrast\n"
                "  auto tone-sniff : on"
            ),
        }
        for widget_id, replacement in replacements.items():
            try:
                widget = screen.query_one(f"#{widget_id}")
            except Exception:
                continue
            widget.update(replacement)
        await pilot.pause()

    return _run()
