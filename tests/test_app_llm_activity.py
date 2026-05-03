"""Pilot tests for the LLM Activity screen (PRD F-LLM-7)."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.main import EpublateApp
from epublate.app.screens.llm_activity import LLMActivityScreen
from epublate.core.project import Project
from epublate.db import repo


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(chapters=[("Solo", "<p>Hello, world.</p>")])
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def _seed_call(
    project: Project,
    *,
    model: str,
    purpose: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    cache_hit: bool = False,
) -> None:
    repo.insert_llm_call(
        project.engine,
        repo.LLMCallRow(
            id=uuid.uuid4().hex,
            project_id=project.project_id,
            segment_id=None,
            purpose=purpose,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            cache_hit=cache_hit,
            cache_key=None,
            request_json="{}",
            response_json="{}",
        ),
    )


@pytest.mark.asyncio
async def test_llm_activity_renders_breakdown(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        _seed_call(
            project,
            model="gpt-5-mini",
            purpose="translate",
            prompt_tokens=200,
            completion_tokens=80,
            cost_usd=0.000_3,
        )
        _seed_call(
            project,
            model="gpt-5-mini",
            purpose="translate",
            prompt_tokens=0,
            completion_tokens=0,
            cost_usd=0.0,
            cache_hit=True,
        )
        _seed_call(
            project,
            model="gpt-4o",
            purpose="extract",
            prompt_tokens=1_200,
            completion_tokens=300,
            cost_usd=0.000_7,
        )

        screen = LLMActivityScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()

            from textual.widgets import DataTable, Static

            totals = screen.query_one("#llm-totals-text", Static)
            text_str = str(totals.render())
            assert "Calls" in text_str
            assert "3" in text_str
            assert "1" in text_str

            model_table = screen.query_one("#llm-by-model-table", DataTable)
            assert model_table.row_count == 2

            purpose_table = screen.query_one("#llm-by-purpose-table", DataTable)
            assert purpose_table.row_count == 2

            recent = screen.query_one("#llm-recent-table", DataTable)
            assert recent.row_count == 3
    finally:
        project.close()


@pytest.mark.asyncio
async def test_llm_activity_handles_empty_project(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = LLMActivityScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import Static

            totals = screen.query_one("#llm-totals-text", Static)
            text_str = str(totals.render())
            assert "no LLM calls" in text_str
    finally:
        project.close()
