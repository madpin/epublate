"""Pilot tests for the M4 Dashboard screen (PRD §4.6 / M4)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.main import EpublateApp
from epublate.app.screens.dashboard import (
    DashboardScreen,
    ExportModal,
    ExportRequest,
    default_export_path,
)
from epublate.app.screens.glossary import GlossaryScreen
from epublate.app.screens.inbox import InboxScreen
from epublate.app.screens.reader import ReaderScreen
from epublate.app.widgets import BatchProgressMeter
from epublate.core.project import Project
from epublate.db import repo
from epublate.llm.mock import MockLLMProvider


def _placeholder_responder() -> Callable[..., str]:
    def _responder(messages: list[object], _model: str) -> str:
        last = messages[-1]
        content = getattr(last, "content", "")
        return json.dumps({"target": f"PT::{content}"})

    return _responder


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
async def test_dashboard_renders_initial_stats(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            assert current.stats is not None
            assert current.stats.segment_count > 0
            # Fresh project has no spend yet.
            assert current.stats.spend_usd == 0.0
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_open_reader_pushes_screen(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ReaderScreen)
            pilot.app.pop_screen()
            await pilot.pause()
            assert isinstance(pilot.app.screen, DashboardScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_open_glossary_and_inbox(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("g")
            await pilot.pause()
            assert isinstance(pilot.app.screen, GlossaryScreen)
            pilot.app.pop_screen()
            await pilot.pause()

            await pilot.press("i")
            await pilot.pause()
            assert isinstance(pilot.app.screen, InboxScreen)
            pilot.app.pop_screen()
            await pilot.pause()
            assert isinstance(pilot.app.screen, DashboardScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_batch_modal_dispatches_worker(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_responder())

        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()

            from epublate.app.screens.dashboard import BatchRequest

            # Bypass the modal — its keystroke flow is tested separately;
            # here we exercise the worker dispatch by injecting the
            # request that the modal would have produced.
            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            current._on_batch_chosen(  # type: ignore[reportPrivateUsage]
                BatchRequest(
                    chapters="*",
                    concurrency=1,
                    model="gpt-mock",
                    budget_usd=None,
                    bypass_cache=False,
                )
            )
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            assert provider.call_count > 0
            stats = current.stats
            assert stats is not None
            assert stats.translated_count > 0
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_batch_keystroke_opens_modal(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    from epublate.app.screens.dashboard import BatchModal

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()
            assert isinstance(pilot.app.screen, BatchModal)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(pilot.app.screen, DashboardScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_batch_modal_pre_pass_default_on(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Submitting the BatchModal with the default form returns ``pre_pass=True``.

    The helper-LLM pre-pass is the cheap proper-noun extractor pass
    that runs before the translator futures; the UI default is *on*
    (PRD §4.2 phase 3 / M5) so curators get glossary growth without
    needing to remember the toggle.
    """

    from textual.widgets import Input

    from epublate.app.screens.dashboard import BatchModal, BatchRequest

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()
            modal = pilot.app.screen
            assert isinstance(modal, BatchModal)
            # The toggle must exist and default to "y" so leaving the
            # form alone enables the pre-pass.
            toggle = modal.query_one("#batch-pre-pass", Input)
            assert toggle.value == "y"

            captured: list[BatchRequest | None] = []
            modal.dismiss = captured.append  # type: ignore[method-assign]
            modal.action_submit()
            assert len(captured) == 1
            request = captured[0]
            assert request is not None
            assert request.pre_pass is True
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_batch_modal_pre_pass_can_be_disabled(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Typing ``n`` in the pre-pass row forwards ``pre_pass=False``."""

    from textual.widgets import Input

    from epublate.app.screens.dashboard import BatchModal, BatchRequest

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()
            modal = pilot.app.screen
            assert isinstance(modal, BatchModal)
            modal.query_one("#batch-pre-pass", Input).value = "n"

            captured: list[BatchRequest | None] = []
            modal.dismiss = captured.append  # type: ignore[method-assign]
            modal.action_submit()
            request = captured[0]
            assert request is not None
            assert request.pre_pass is False
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_batch_dispatch_resolves_helper_model_from_project_override(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """``_on_batch_chosen`` resolves the helper model from the project's
    LLM overrides and threads it onto :class:`BatchOptions` so the
    pre-pass actually targets the cheap model the curator configured."""

    from epublate.app.screens.dashboard import BatchRequest
    from epublate.core.batch import BatchOptions

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        repo.set_llm_overrides(
            project.engine,
            project_id=project.project_id,
            overrides={"helper_model": "gpt-cheap-helper"},
        )
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_responder())
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)

        captured_options: list[BatchOptions] = []

        def _capture_start_batch(*, options: BatchOptions, **_: object) -> bool:
            captured_options.append(options)
            return False  # Skip the actual worker; we only care about wiring.

        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            pilot.app.start_batch = _capture_start_batch  # type: ignore[method-assign]
            current._on_batch_chosen(  # type: ignore[reportPrivateUsage]
                BatchRequest(
                    chapters="*",
                    concurrency=1,
                    model="gpt-mock",
                    budget_usd=None,
                    bypass_cache=False,
                    pre_pass=True,
                )
            )
            await pilot.pause()
            assert len(captured_options) == 1
            options = captured_options[0]
            assert options.pre_pass is True
            assert options.helper_model == "gpt-cheap-helper"
            assert options.model == "gpt-mock"
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_batch_dispatch_pre_pass_off_skips_helper_resolution(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """When the curator opts the pre-pass off, the dispatch leaves
    ``helper_model=None`` so :func:`run_batch` has no helper config to
    apply (and the no-op pre-pass branch is the cheapest possible)."""

    from epublate.app.screens.dashboard import BatchRequest
    from epublate.core.batch import BatchOptions

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_responder())
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)

        captured_options: list[BatchOptions] = []

        def _capture_start_batch(*, options: BatchOptions, **_: object) -> bool:
            captured_options.append(options)
            return False

        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            pilot.app.start_batch = _capture_start_batch  # type: ignore[method-assign]
            current._on_batch_chosen(  # type: ignore[reportPrivateUsage]
                BatchRequest(
                    chapters="*",
                    concurrency=1,
                    model="gpt-mock",
                    budget_usd=None,
                    bypass_cache=False,
                    pre_pass=False,
                )
            )
            await pilot.pause()
            options = captured_options[0]
            assert options.pre_pass is False
            assert options.helper_model is None
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_batch_modal_renders_project_stats(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The BatchModal's stats panel shows chapter/segment counts."""

    from textual.widgets import Static

    from epublate.app.screens.dashboard import BatchModal

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("b")
            await pilot.pause()
            assert isinstance(pilot.app.screen, BatchModal)
            stats_widget = pilot.app.screen.query_one("#batch-stats", Static)
            content = str(stats_widget.render())
            assert "Project stats" in content
            assert "Chapters" in content
            assert "Segments" in content
            assert "Avg / median" in content
            await pilot.press("escape")
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_batch_run_publishes_progress_to_app(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """While the batch worker runs the dashboard surfaces the progress meter
    and writes the live snapshot onto the app slot the Reader watches."""

    from epublate.app.screens.dashboard import BatchRequest

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_responder())
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            current._on_batch_chosen(  # type: ignore[reportPrivateUsage]
                BatchRequest(
                    chapters="*",
                    concurrency=1,
                    model="gpt-mock",
                    budget_usd=None,
                    bypass_cache=False,
                )
            )
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            meter = pilot.app.screen.query_one(
                "#dashboard-batch-meter", BatchProgressMeter
            )
            content = str(meter.render())
            assert "Batch" in content
            # After the batch completes the meter should show the final
            # tally — total ≥ attempted, and the bar reaches the right edge.
            assert provider.call_count > 0
            assert pilot.app.batch_progress.summary is not None  # type: ignore[attr-defined]
            assert pilot.app.batch_progress.active is False  # type: ignore[attr-defined]
            # Chapter count + project total cost are surfaced on the
            # snapshot so curators can see "across N chapters" and
            # "this batch · project total" at a glance.
            progress = pilot.app.batch_progress  # type: ignore[attr-defined]
            assert progress.chapter_count >= 1
            assert progress.starting_spend_usd == 0.0
            content_with_chapters = str(meter.render())
            assert "chapter" in content_with_chapters.lower()
            assert "project total" in content_with_chapters
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_batch_keeps_running_after_screen_pop(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A batch dispatched on the Dashboard survives popping back to Projects.

    The App owns the worker thread and the originating Project (which
    keeps the SQLite engine alive); popping the Dashboard does NOT
    cancel the batch. This pins down PRD §4.6 / §7.3 — the curator
    can leave the project and come back to a still-progressing run.
    """

    from epublate.app.screens.dashboard import BatchRequest
    from epublate.app.screens.projects import ProjectsScreen

    src = tiny_epub_factory(
        chapters=[
            ("Solo", "<p>Para one.</p><p>Para two.</p><p>Para three.</p>"),
        ]
    )
    project = Project.create(
        src,
        out_dir=tmp_path / "persist-proj",
        source_lang="en",
        target_lang="pt",
    )
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_responder())
        # Land on Projects first, then push the Dashboard, so popping the
        # Dashboard returns to a real screen the App will keep mounted.
        landing = ProjectsScreen()
        app = EpublateApp(initial_screen=landing)
        async with app.run_test() as pilot:
            await pilot.pause()
            dashboard = DashboardScreen(
                project,
                provider_factory=lambda: provider,
                default_model="gpt-mock",
            )
            pilot.app.push_screen(dashboard)
            await pilot.pause()
            assert isinstance(pilot.app.screen, DashboardScreen)
            dashboard._on_batch_chosen(  # type: ignore[reportPrivateUsage]
                BatchRequest(
                    chapters="*",
                    concurrency=1,
                    model="gpt-mock",
                    budget_usd=None,
                    bypass_cache=False,
                )
            )
            assert pilot.app.batch_running  # type: ignore[attr-defined]
            pilot.app.pop_screen()
            await pilot.pause()
            assert isinstance(pilot.app.screen, ProjectsScreen)
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert provider.call_count > 0
            assert not pilot.app.batch_running  # type: ignore[attr-defined]
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_cancel_batch_stops_worker(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Pressing 'c' on the Dashboard asks the App to cancel the running batch.

    Cancellation is best-effort: in-flight LLM calls finish, no new
    ones are submitted. The final ``batch_progress`` snapshot is
    inactive (the worker drained) and ``batch_running`` is false.
    """

    from epublate.app.screens.dashboard import BatchRequest

    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "".join(f"<p>Para {n}.</p>" for n in range(8)),
            ),
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "cancel-proj", source_lang="en", target_lang="pt"
    )
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_responder())
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            dashboard = pilot.app.screen
            assert isinstance(dashboard, DashboardScreen)
            dashboard._on_batch_chosen(  # type: ignore[reportPrivateUsage]
                BatchRequest(
                    chapters="*",
                    concurrency=1,
                    model="gpt-mock",
                    budget_usd=None,
                    bypass_cache=False,
                    group_small_segments=False,
                )
            )
            cancelled = pilot.app.cancel_batch()  # type: ignore[attr-defined]
            assert cancelled is True
            assert pilot.app.batch_progress.cancelling  # type: ignore[attr-defined]
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()
            assert not pilot.app.batch_running  # type: ignore[attr-defined]
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_set_budget_updates_meter(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("B")  # uppercase = budget modal
            await pilot.pause()
            # Type a budget and submit.
            await pilot.press(*"5.00")
            await pilot.press("ctrl+s")
            await pilot.pause()

            row = repo.get_project(project.engine, project.project_id)
            assert row is not None
            assert row.budget_usd == 5.00

            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            assert current.stats is not None
            assert current.stats.budget_usd == 5.00
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_intake_keystroke_opens_modal(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The 'e' binding must open the IntakeModal, not block on workers."""

    from epublate.app.screens.dashboard import IntakeModal

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(pilot.app.screen, IntakeModal)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(pilot.app.screen, DashboardScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_intake_dispatches_worker(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """End-to-end: the intake worker proposes entries and refreshes stats."""

    from epublate.app.screens.dashboard import IntakeRequest

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_response(
            json.dumps(
                {
                    "entities": [
                        {"type": "character", "source": "Hero"},
                        {"type": "place", "source": "Citadel"},
                    ],
                    "pov": "third_limited",
                }
            )
        )
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            current._on_intake_chosen(  # type: ignore[reportPrivateUsage]
                IntakeRequest(helper_model="gpt-mock-helper", max_segments=5)
            )
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            assert current.intake_running is False

            proposed = repo.list_glossary_entries(
                project.engine, project.project_id, status="proposed"
            )
            assert any(p.source_term == "Hero" for p in proposed)
            assert any(p.source_term == "Citadel" for p in proposed)
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Back-out via Escape (q + esc both pop the screen) — PRD §4.6
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_escape_pops_back(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Escape on a child screen pops back to the Dashboard, mirroring `q`."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("g")
            await pilot.pause()
            assert isinstance(pilot.app.screen, GlossaryScreen)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(pilot.app.screen, DashboardScreen)
    finally:
        project.close()


def test_glossary_screen_has_hidden_escape_back_binding() -> None:
    """Esc → Back is registered but ``show=False`` so the footer stays clean."""

    from textual.binding import Binding

    from epublate.app.screens.glossary import GlossaryScreen as _Glossary

    bindings = [b for b in _Glossary.BINDINGS if isinstance(b, Binding)]
    matches = [
        b for b in bindings if b.key == "escape" and b.action == "app.pop_screen"
    ]
    assert matches, "GlossaryScreen should bind Escape to app.pop_screen"
    assert all(not b.show for b in matches), (
        "the Escape→Back binding must stay hidden from the footer"
    )


def test_back_out_screens_have_hidden_escape_binding() -> None:
    """Every screen that exposes ``q Back`` also accepts Escape silently."""

    from textual.binding import Binding

    from epublate.app.screens.dashboard import DashboardScreen as _Dashboard
    from epublate.app.screens.glossary import GlossaryScreen as _Glossary
    from epublate.app.screens.inbox import InboxScreen as _Inbox
    from epublate.app.screens.reader import ReaderScreen as _Reader
    from epublate.app.screens.settings import SettingsScreen as _Settings

    for cls in (_Dashboard, _Reader, _Glossary, _Inbox, _Settings):
        bindings = [b for b in cls.BINDINGS if isinstance(b, Binding)]
        q_back = [b for b in bindings if b.key == "q" and b.action == "app.pop_screen"]
        esc_back = [
            b for b in bindings if b.key == "escape" and b.action == "app.pop_screen"
        ]
        assert q_back, f"{cls.__name__} should bind q to app.pop_screen"
        assert esc_back, f"{cls.__name__} should also bind Escape to app.pop_screen"
        assert all(not b.show for b in esc_back), (
            f"{cls.__name__}'s Escape→Back binding must be hidden from the footer"
        )


# ---------------------------------------------------------------------------
# Export flow (PRD §7.6 / F-IO-7) — save the (possibly partial) ePub
# ---------------------------------------------------------------------------


def test_default_export_path_lives_inside_project_dir(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        path = default_export_path(project)
        assert path.parent == project.project_dir
        assert path.suffix == ".epub"
        # Filename includes the target language so re-translating to a
        # different language doesn't overwrite an earlier export.
        assert ".pt." in path.name or path.name.endswith(".pt.epub")
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_x_keystroke_opens_export_modal(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ExportModal)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(pilot.app.screen, DashboardScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_export_modal_explains_partial_save_behavior(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The modal copy must make the partial-save story unmistakable."""

    from textual.widgets import Static

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ExportModal)
            info = pilot.app.screen.query_one("#export-info", Static)
            content = str(info.render())
            # The hint must call out (a) anytime save and (b) source-text fallback.
            assert "Save anytime" in content
            assert "source text" in content
            await pilot.press("escape")
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_export_worker_writes_partial_epub(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Even with zero translated segments, the export must produce a valid ePub
    file using the source text fallback (PRD F-IO-7)."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            out_path = tmp_path / "exports" / "partial.epub"
            out_path.parent.mkdir(parents=True, exist_ok=True)

            current._on_export_chosen(  # type: ignore[reportPrivateUsage]
                ExportRequest(out_path=out_path, epubcheck=False)
            )
            await pilot.app.workers.wait_for_complete()
            await pilot.pause()

            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            assert current.export_running is False
            assert out_path.is_file()
            assert out_path.stat().st_size > 0
    finally:
        project.close()


@pytest.mark.asyncio
async def test_export_modal_rejects_non_epub_suffix(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Submitting a path without an .epub suffix surfaces an error in-place."""

    from textual.widgets import Input, Static

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(
            project,
            provider_factory=lambda: provider,
            default_model="gpt-mock",
        )
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ExportModal)
            path_input = pilot.app.screen.query_one("#export-path", Input)
            path_input.value = str(tmp_path / "not-an-epub.txt")
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            # Modal stays open and the inline error is populated.
            assert isinstance(pilot.app.screen, ExportModal)
            error = pilot.app.screen.query_one("#export-error", Static)
            content = str(error.render())
            assert ".epub" in content
            await pilot.press("escape")
    finally:
        project.close()


# ---------------------------------------------------------------------------
# Phase 1: dashboard book panel + chapter table + glossary stats + intake
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_book_panel_shows_metadata(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The Book panel renders title / author / langs / counts from the OPF."""

    from textual.widgets import Static

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(project, provider_factory=MockLLMProvider)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            meta_widget = current.query_one("#dashboard-book-meta", Static)
            text = str(meta_widget.render())
            # tiny_epub_factory pins the title; sanity-check we read it.
            assert "title" in text
            assert "author(s)" in text
            assert "en → pt" in text
            assert "scope" in text
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_chapter_table_lists_chapters(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The chapter table mounts with a row per imported chapter."""

    from textual.widgets import DataTable

    src = tiny_epub_factory(
        chapters=[
            ("Chapter One", "<p>Intro.</p>"),
            ("Chapter Two", "<p>Middle.</p>"),
            ("Chapter Three", "<p>End.</p>"),
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "multi-proj", source_lang="en", target_lang="pt"
    )
    try:
        screen = DashboardScreen(project, provider_factory=MockLLMProvider)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            table = current.query_one("#dashboard-chapter-table", DataTable)
            assert table.row_count >= 3
            assert {col.label.plain for col in table.columns.values()} >= {
                "#",
                "Chapter",
                "Segs",
                "% Trans",
                "% Approved",
            }
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_glossary_panel_shows_zero_state(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A fresh project shows the empty-glossary nudge."""

    from textual.widgets import Static

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(project, provider_factory=MockLLMProvider)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            panel = current.query_one("#dashboard-glossary-panel", Static)
            text = str(panel.render())
            assert "Glossary" in text
            assert "no entries yet" in text
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_intake_status_defaults_to_not_run(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The intake header strip teaches the curator what intake does."""

    from textual.widgets import Static

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(project, provider_factory=MockLLMProvider)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            widget = current.query_one("#dashboard-intake-status", Static)
            text = str(widget.render())
            assert "Intake" in text
            assert "not run" in text
            assert "lore bible" in text
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_intake_status_shows_summary_after_event(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """An ``intake.completed`` event renders chunks/proposed counts."""

    from textual.widgets import Static

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        repo.append_event(
            project.engine,
            project_id=project.project_id,
            kind="intake.completed",
            payload={
                "chunks": 12,
                "proposed_count": 7,
                "cost_usd": 0.0123,
                "pov": "third",
                "tense": "past",
            },
        )
        screen = DashboardScreen(project, provider_factory=MockLLMProvider)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            widget = pilot.app.screen.query_one("#dashboard-intake-status", Static)
            text = str(widget.render())
            assert "12 chunks" in text
            assert "7 proposed" in text
            assert "$0.0123" in text
            assert "pov=third" in text
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_llm_activity_table_empty_state(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """No LLM calls yet → the table renders a single placeholder row."""

    from textual.widgets import DataTable

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(project, provider_factory=MockLLMProvider)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            table = pilot.app.screen.query_one(
                "#dashboard-llm-activity-table", DataTable
            )
            assert table.row_count == 1
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_chapter_row_enter_opens_reader(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Selecting a chapter row pushes Reader at the chosen chapter id."""

    from textual.widgets import DataTable

    src = tiny_epub_factory(
        chapters=[
            ("Chapter One", "<p>Intro.</p>"),
            ("Chapter Two", "<p>Middle.</p>"),
        ]
    )
    project = Project.create(
        src, out_dir=tmp_path / "row-proj", source_lang="en", target_lang="pt"
    )
    try:
        screen = DashboardScreen(project, provider_factory=MockLLMProvider)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, DashboardScreen)
            table = current.query_one("#dashboard-chapter-table", DataTable)
            table.focus()
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(pilot.app.screen, ReaderScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_auto_intake_on_first_mount_when_enabled(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """``auto_intake_on_first_mount=True`` surfaces the IntakeModal once."""

    from epublate.app.screens.dashboard import IntakeModal

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(
            project,
            provider_factory=MockLLMProvider,
            auto_intake_on_first_mount=True,
        )
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            assert isinstance(pilot.app.screen, IntakeModal)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(pilot.app.screen, DashboardScreen)
            # The auto-trigger fires only once: pressing R (refresh)
            # must not re-summon the modal.
            await pilot.press("r")
            await pilot.pause()
            assert isinstance(pilot.app.screen, DashboardScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_auto_intake_skipped_when_intake_already_ran(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """If an intake event already exists we do *not* re-run automatically."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        repo.append_event(
            project.engine,
            project_id=project.project_id,
            kind="intake.completed",
            payload={"chunks": 1, "proposed_count": 0, "cost_usd": 0.0},
        )
        screen = DashboardScreen(
            project,
            provider_factory=MockLLMProvider,
            auto_intake_on_first_mount=True,
        )
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.pause()
            assert isinstance(pilot.app.screen, DashboardScreen)
    finally:
        project.close()
