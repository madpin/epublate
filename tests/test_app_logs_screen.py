"""LogsScreen behavioural smoke tests (PRD §11)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.log_buffer import RingBufferHandler
from epublate.app.main import EpublateApp
from epublate.app.screens.dashboard import DashboardScreen
from epublate.app.screens.logs import LogsScreen, _summarize_event_payload
from epublate.core.project import Project
from epublate.db import repo
from epublate.llm.mock import MockLLMProvider


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(chapters=[("Solo", "<h1>Solo</h1><p>Hello.</p>")])
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


@pytest.mark.asyncio
async def test_logs_screen_renders_events_and_log_records(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        # Pre-seed an event row + a logger record so the screen has
        # data from both sources to merge.
        repo.append_event(
            project.engine,
            project_id=project.project_id,
            kind="batch.completed",
            payload={
                "translated": 5,
                "cached": 1,
                "flagged": 0,
                "cost_usd": 0.1234,
            },
        )
        screen = DashboardScreen(project, provider_factory=lambda: MockLLMProvider())
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            buf = pilot.app.log_buffer  # type: ignore[attr-defined]
            assert isinstance(buf, RingBufferHandler)
            # Push a log line into the buffer the screen will read.
            logging.getLogger("epublate.test_logs").error("synthetic failure")
            # Press the new ``l`` binding from the Dashboard.
            await pilot.press("l")
            await pilot.pause()
            assert isinstance(pilot.app.screen, LogsScreen)
            current = pilot.app.screen
            # Default filter shows events + logs. Both rows should be visible.
            sources = {row.source for row in current._rows}  # type: ignore[reportPrivateUsage]
            assert "event" in sources
            assert "log" in sources
    finally:
        project.close()


@pytest.mark.asyncio
async def test_logs_screen_cycle_source_filter_includes_llm(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        # Seed a fake llm_call so we can verify the LLM source toggles in.
        repo.insert_llm_call(
            project.engine,
            repo.LLMCallRow(
                id="llm-1",
                project_id=project.project_id,
                segment_id=None,
                purpose="translate",
                model="gpt-mock",
                prompt_tokens=100,
                completion_tokens=50,
                cost_usd=0.001,
                cache_hit=False,
                cache_key="k",
                request_json="{}",
                response_json="{}",
            ),
        )
        screen = LogsScreen(project)
        app = EpublateApp(initial_screen=screen)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, LogsScreen)
            # LLM filter is off in the default view. Calling the action
            # directly avoids the focus-handoff issue with the search
            # Input that swallows raw keypresses.
            current.action_cycle_source()  # → all
            await pilot.pause()
            assert "llm" in {r.source for r in current._rows}  # type: ignore[reportPrivateUsage]
    finally:
        project.close()


def test_summarize_event_payload_handles_known_kinds() -> None:
    """The summary helper extracts a few well-known fields cleanly."""

    ev = repo.EventRow(
        id=1,
        project_id="p",
        ts=0,
        kind="batch.segment_failed",
        payload={"segment_id": "seg-1", "error": "boom"},
    )
    out = _summarize_event_payload(ev)
    assert "boom" in out
    assert "seg-1" in out


def test_summarize_event_payload_falls_back_to_kv_dump() -> None:
    """Unrecognised events still produce a useful one-line summary."""

    ev = repo.EventRow(
        id=2,
        project_id="p",
        ts=0,
        kind="custom.kind",
        payload={"x": 1, "y": 2},
    )
    out = _summarize_event_payload(ev)
    assert "x=1" in out
    assert "y=2" in out
