"""Form modals must accept ``Enter`` and ``Ctrl+S`` to submit (TUI rule)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.main import EpublateApp
from epublate.app.screens.dashboard import (
    BatchModal,
    BatchRequest,
    BudgetModal,
    DashboardScreen,
    IntakeModal,
    IntakeRequest,
)
from epublate.app.screens.glossary import (
    EntryEditScreen,
    GlossaryScreen,
    _EntryDraft,
    _EntryDraftResult,
)
from epublate.app.screens.lore_book_dashboard import (
    IngestRequest,
    LoreIngestModal,
)
from epublate.app.screens.lore_books import (
    NewLoreBookModal,
    NewLoreBookResult,
)
from epublate.core.project import Project
from epublate.db.schema import LoreSourceKind
from epublate.llm.mock import MockLLMProvider


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(chapters=[("Solo", "<h1>Solo</h1><p>Hello.</p>")])
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


@pytest.mark.asyncio
async def test_batch_modal_enter_submits(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        screen = DashboardScreen(project, provider_factory=lambda: provider)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            captured: list[BatchRequest | None] = []
            modal = BatchModal(default_model="gpt-mock")
            await pilot.app.push_screen(modal, captured.append)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert captured, "Enter must dismiss the modal with a request"
            assert isinstance(captured[0], BatchRequest)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_budget_modal_enter_submits(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(project, provider_factory=lambda: MockLLMProvider())
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            from epublate.app.screens.dashboard import BudgetRequest

            captured: list[BudgetRequest | None] = []
            modal = BudgetModal(current_budget=None)
            await pilot.app.push_screen(modal, captured.append)
            await pilot.pause()
            from textual.widgets import Input

            modal.query_one("#budget-input", Input).value = "1.50"
            await pilot.press("enter")
            await pilot.pause()
            assert captured and captured[0] is not None
            assert captured[0].budget_usd == pytest.approx(1.50)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_intake_modal_enter_submits(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(project, provider_factory=lambda: MockLLMProvider())
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            captured: list[IntakeRequest | None] = []
            modal = IntakeModal(
                default_helper_model="gpt-mock", default_max_segments=10
            )
            await pilot.app.push_screen(modal, captured.append)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert captured and isinstance(captured[0], IntakeRequest)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_glossary_entry_modal_enter_submits(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        glossary_screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=glossary_screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            captured: list[_EntryDraftResult | None] = []
            draft = _EntryDraft(
                source_term="House Stark",
                target_term="Casa Stark",
            )
            modal = EntryEditScreen(draft, title="New entry")
            await pilot.app.push_screen(modal, captured.append)
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            assert captured and isinstance(captured[0], _EntryDraftResult)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_lore_ingest_modal_enter_submits(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The modal validates the ePub path; we feed it a real path so
    Enter actually dismisses with a request rather than ringing the bell."""

    src = tiny_epub_factory(chapters=[("S", "<p>x</p>")])
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        glossary_screen = GlossaryScreen(project)
        app = EpublateApp(initial_screen=glossary_screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            captured: list[IngestRequest | None] = []
            modal = LoreIngestModal(
                kind=LoreSourceKind.SOURCE,
                default_helper_model="gpt-mock",
                default_max_units=5,
            )
            await pilot.app.push_screen(modal, captured.append)
            await pilot.pause()
            from textual.widgets import Input

            modal.query_one("#lore-ingest-path", Input).value = str(src)
            await pilot.press("enter")
            await pilot.pause()
            assert captured and isinstance(captured[0], IngestRequest)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_new_lore_book_modal_enter_submits(
    tmp_path: Path,
) -> None:
    app = EpublateApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        captured: list[NewLoreBookResult | None] = []
        modal = NewLoreBookModal(library_dir=tmp_path)
        await pilot.app.push_screen(modal, captured.append)
        await pilot.pause()
        from textual.widgets import Input

        modal.query_one("#lore-new-name", Input).value = "Test Lore"
        await pilot.press("enter")
        await pilot.pause()
        assert captured and isinstance(captured[0], NewLoreBookResult)
