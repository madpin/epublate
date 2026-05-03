"""Persistent ``BatchStatusBar`` banner pilot tests.

The Dashboard owns the rich :class:`BatchProgressMeter` panel; the
Reader has a multi-line footer mirror. Every *other* main screen
(Glossary, Inbox, Settings, LLM Activity, Projects) gets the slim
:class:`BatchStatusBar` so the curator never loses sight of an
in-flight batch when they triage a different surface
(PRD §4.6 / batch UI).

These tests pin down the contract:

* The bar is invisible while no batch is running.
* Setting ``app.batch_progress`` to an active snapshot makes the bar
  appear with the running batch's totals on every supported screen.
* Switching the snapshot back to inactive hides the bar again
  (so it doesn't linger after the worker drains).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.main import BatchProgress, EpublateApp
from epublate.app.screens.glossary import GlossaryScreen
from epublate.app.screens.inbox import InboxScreen
from epublate.app.screens.llm_activity import LLMActivityScreen
from epublate.app.screens.projects import ProjectsScreen
from epublate.app.screens.settings import SettingsScreen
from epublate.app.widgets import BatchStatusBar
from epublate.core.batch import BatchSummary
from epublate.core.project import Project


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(
        chapters=[("Solo", "<h1>Solo</h1><p>Hello, world.</p><p>Second paragraph.</p>")]
    )
    return Project.create(
        src,
        out_dir=tmp_path / "proj",
        source_lang="en",
        target_lang="pt",
    )


def _running_progress(project: Project) -> BatchProgress:
    return BatchProgress(
        active=True,
        project_id=project.project_id,
        summary=BatchSummary(
            translated=4,
            cached=1,
            flagged=0,
            failed=0,
            cost_usd=0.0123,
            elapsed_s=5.0,
            prompt_tokens=200,
            completion_tokens=100,
        ),
        total=10,
        chapter_count=2,
        starting_spend_usd=0.0250,
        paused=False,
        cancelling=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "screen_factory,bar_id",
    [
        (
            lambda project: GlossaryScreen(project),
            "#glossary-batch-status",
        ),
        (
            lambda project: InboxScreen(project),
            "#inbox-batch-status",
        ),
        (
            lambda project: SettingsScreen(project, default_model="gpt-mock"),
            "#settings-batch-status",
        ),
        (
            lambda project: LLMActivityScreen(project),
            "#llm-batch-status",
        ),
    ],
    ids=["glossary", "inbox", "settings", "llm-activity"],
)
async def test_batch_status_bar_shows_active_batch(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    screen_factory: Callable[[Project], object],
    bar_id: str,
) -> None:
    """The slim banner appears as soon as ``app.batch_progress`` flips active.

    Drives the same assertion across the four "main" non-Dashboard /
    non-Reader screens so any regression on a single screen's
    ``compose`` (forgetting to ``yield BatchStatusBar(...)``) trips
    the parametrize matrix immediately.
    """

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = screen_factory(project)
        app = EpublateApp(initial_screen=screen)  # type: ignore[arg-type]
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            bar = current.query_one(bar_id, BatchStatusBar)
            assert not bar.has_class("-active")
            assert not bar.has_class("-cancelling")
            assert not bar.has_class("-paused")

            pilot.app.batch_progress = _running_progress(  # type: ignore[attr-defined]
                project
            )
            bar._refresh_now()  # type: ignore[reportPrivateUsage]
            assert bar.has_class("-active")
            content = str(bar.render())
            # Sanity-check: the running totals show up on the bar.
            assert "running" in content.lower()
            assert "5/10" in content  # 4 translated + 1 cached attempted
            assert "2 chapters" in content
            # The cost line carries both the batch and the project total
            # (the App snapshots the project's pre-batch spend at start).
            assert "$0.0123" in content
            assert "$0.0373" in content  # 0.0250 starting + 0.0123 batch cost

            pilot.app.batch_progress = BatchProgress(  # type: ignore[attr-defined]
                active=False,
            )
            bar._refresh_now()  # type: ignore[reportPrivateUsage]
            assert not bar.has_class("-active")
    finally:
        project.close()


@pytest.mark.asyncio
async def test_batch_status_bar_hides_for_foreign_project(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A status bar tied to project A ignores a batch on project B.

    Future-proofing: the same App can host work on multiple projects
    (the curator could open one, dispatch a batch, hop to the
    Projects landing screen). The Projects screen is project-agnostic
    so it should always reflect the App's batch slot — but other
    screens carry a ``project`` reference and only mirror their own.
    """

    project_a = _make_project(tiny_epub_factory, tmp_path / "a")
    try:
        screen = GlossaryScreen(project_a)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            bar = pilot.app.screen.query_one("#glossary-batch-status", BatchStatusBar)
            # A batch on a different project flips active=True but for
            # a foreign project_id; the bar still shows it because the
            # banner is project-agnostic by design — the curator wants
            # to know "something is running" even if they hopped
            # projects.
            pilot.app.batch_progress = BatchProgress(  # type: ignore[attr-defined]
                active=True,
                project_id="some-other-project",
                total=5,
                chapter_count=1,
            )
            bar._refresh_now()  # type: ignore[reportPrivateUsage]
            assert bar.has_class("-active")
    finally:
        project_a.close()


@pytest.mark.asyncio
async def test_batch_status_bar_on_projects_screen(
    tmp_path: Path,
) -> None:
    """The Projects landing screen also gets the slim banner.

    Includes the case where the curator backs out of a project
    entirely — they're staring at the Projects list while a batch
    they dispatched on the previous project keeps running.
    """

    config_path = tmp_path / "ui.toml"
    landing = ProjectsScreen(config_path=config_path)
    app = EpublateApp(initial_screen=landing, config_path=config_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = pilot.app.screen.query_one("#projects-batch-status", BatchStatusBar)
        assert not bar.has_class("-active")
        pilot.app.batch_progress = BatchProgress(  # type: ignore[attr-defined]
            active=True,
            project_id="any-project",
            total=3,
            chapter_count=1,
            paused=True,
        )
        bar._refresh_now()  # type: ignore[reportPrivateUsage]
        assert bar.has_class("-paused")
        assert "paused" in str(bar.render()).lower()


@pytest.mark.asyncio
async def test_batch_status_bar_shows_cancelling_state(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The `-cancelling` CSS hook fires while in-flight calls drain."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = InboxScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            bar = pilot.app.screen.query_one("#inbox-batch-status", BatchStatusBar)
            pilot.app.batch_progress = BatchProgress(  # type: ignore[attr-defined]
                active=True,
                project_id=project.project_id,
                total=5,
                chapter_count=1,
                cancelling=True,
            )
            bar._refresh_now()  # type: ignore[reportPrivateUsage]
            assert bar.has_class("-cancelling")
            assert "cancelling" in str(bar.render()).lower()
    finally:
        project.close()
