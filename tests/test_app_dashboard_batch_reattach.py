"""Dashboard re-attaches to a running batch after a remount.

Regression coverage for the user-reported bug:

> If I leave a project, and go back, … if I had a batch translation
> running, it does not show the progress anymore.

The fix lives in :meth:`DashboardScreen.on_mount` /
:meth:`DashboardScreen.on_unmount` — when the App holds an active
:class:`BatchProgress` slot for the project the new Dashboard
belongs to, the screen re-registers as the batch listener and
unhides the rich progress panel.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.main import BatchProgress, EpublateApp
from epublate.app.screens.dashboard import DashboardScreen
from epublate.core.batch import BatchSummary
from epublate.core.project import Project
from epublate.llm.mock import MockLLMProvider


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1><p>Hello, world.</p><p>Second paragraph.</p>",
            )
        ]
    )
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


@pytest.mark.asyncio
async def test_dashboard_reattaches_to_running_batch_on_mount(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A fresh Dashboard mounted while a batch is in flight re-binds
    the listener and shows the rich progress panel."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            # Simulate "a batch is running for THIS project" without
            # actually dispatching a worker — the test isolates the
            # re-mount behaviour from the worker thread.
            pilot.app.batch_progress = BatchProgress(  # type: ignore[attr-defined]
                active=True,
                project_id=project.project_id,
                summary=BatchSummary(
                    translated=2,
                    cached=0,
                    flagged=0,
                    failed=0,
                    elapsed_s=1.0,
                    cost_usd=0.0,
                ),
                total=10,
                chapter_count=1,
                starting_spend_usd=0.0,
            )
            # Force a new Dashboard to mount on top so we exercise
            # ``on_mount``'s re-attach branch.
            new_dash = DashboardScreen(project, provider_factory=lambda: provider)
            await pilot.app.push_screen(new_dash)
            await pilot.pause()
            assert pilot.app.screen is new_dash
            # The slim status bar shows the App-level batch state.
            from epublate.app.widgets import BatchStatusBar

            bar = new_dash.query_one("#dashboard-batch-status", BatchStatusBar)
            # The bar's display flag flips on for any active batch.
            assert "-active" in bar.classes
            # The dashboard itself thinks a batch is running, which
            # is what gates the Cancel binding.
            assert new_dash._batch_running is True  # type: ignore[reportPrivateUsage]
            # And it registered itself as the listener.
            assert pilot.app._batch_listener is new_dash  # type: ignore[reportPrivateUsage]
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_does_not_reattach_to_other_projects_batch(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A batch for project B must NOT show on project A's Dashboard
    rich panel — the slim BatchStatusBar carries cross-project state."""

    project_a = _make_project(tiny_epub_factory, tmp_path)
    project_b = _make_project(tiny_epub_factory, tmp_path / "b")
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(project_a, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            pilot.app.batch_progress = BatchProgress(  # type: ignore[attr-defined]
                active=True,
                project_id=project_b.project_id,
                summary=None,
                total=5,
                chapter_count=1,
            )
            new_dash = DashboardScreen(project_a, provider_factory=lambda: provider)
            await pilot.app.push_screen(new_dash)
            await pilot.pause()
            # Rich panel is project-scoped: stays hidden when the
            # batch belongs to a different project.
            assert new_dash._batch_running is False  # type: ignore[reportPrivateUsage]
            assert pilot.app._batch_listener is not new_dash  # type: ignore[reportPrivateUsage]
    finally:
        project_a.close()
        project_b.close()


@pytest.mark.asyncio
async def test_dashboard_detaches_listener_on_unmount(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Popping the Dashboard releases the App's listener slot so the
    worker doesn't keep posting messages into a freed Screen."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            pilot.app.attach_batch_listener(pilot.app.screen)  # type: ignore[arg-type]
            assert pilot.app._batch_listener is pilot.app.screen  # type: ignore[reportPrivateUsage]
            pilot.app.pop_screen()
            await pilot.pause()
            assert pilot.app._batch_listener is None  # type: ignore[reportPrivateUsage]
    finally:
        project.close()
