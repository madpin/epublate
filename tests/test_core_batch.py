"""Tests for the M4 batch translation runner (PRD §7.3)."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.core.batch import (
    BatchCancelled,
    BatchOptions,
    BatchPaused,
    BatchProgressEvent,
    run_batch,
)
from epublate.core.project import Project
from epublate.core.stats import pending_segments
from epublate.db import repo
from epublate.db.schema import SegmentStatus
from epublate.errors import LLMResponseError
from epublate.llm.mock import MockLLMProvider


def _placeholder_safe_responder() -> Callable[..., str]:
    def _responder(messages: list[object], _model: str) -> str:
        last = messages[-1]
        content = getattr(last, "content", "")
        return json.dumps(
            {
                "target": f"PT::{content}",
                "used_entries": [],
                "new_entities": [],
            }
        )

    return _responder


def _make_project(
    tiny_factory: Callable[..., Path], tmp_path: Path, *, multi: bool = True
) -> Project:
    chapters: list[tuple[str, str]] = (
        [
            (
                "C1",
                "<h1>C1</h1><p>Hello, world.</p><p>Second paragraph.</p>",
            ),
            (
                "C2",
                "<h1>C2</h1><p>Third paragraph.</p><p>Fourth paragraph.</p>",
            ),
        ]
        if multi
        else [
            ("Solo", "<h1>Solo</h1><p>Hello, world.</p><p>Second.</p>"),
        ]
    )
    src = tiny_factory(chapters=chapters)
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


def test_run_batch_translates_pending_segments(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())

        before = pending_segments(project.engine, project_id=project.project_id)
        assert before, "fixture must have pending segments"

        summary = run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(model="gpt-mock"),
        )

        assert summary.failed == 0
        assert summary.attempted == len(before)
        assert summary.translated == len(before)

        after = pending_segments(project.engine, project_id=project.project_id)
        assert not after

        events = repo.list_events(project.engine, project.project_id)
        kinds = {e.kind for e in events}
        assert "batch.started" in kinds
        assert "batch.completed" in kinds
    finally:
        project.close()


def test_run_batch_concurrency_does_not_change_outcome(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path, multi=True)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())

        summary = run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(model="gpt-mock", concurrency=4),
        )
        assert summary.translated > 0
        assert summary.failed == 0
        # Every segment should now have a target stored.
        for chap in repo.list_chapters(project.engine, project.project_id):
            for seg in repo.list_segments(project.engine, chap.id):
                assert seg.target_text is not None
                assert seg.status == SegmentStatus.TRANSLATED
    finally:
        project.close()


def test_run_batch_progress_callback_receives_every_segment(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())

        ticks: list[BatchProgressEvent] = []
        lock = threading.Lock()

        def _on_progress(ev: BatchProgressEvent) -> None:
            with lock:
                ticks.append(ev)

        summary = run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(model="gpt-mock", concurrency=2),
            on_progress=_on_progress,
        )

        assert len(ticks) == summary.attempted
        assert all(t.outcome is not None for t in ticks)
    finally:
        project.close()


def test_run_batch_pauses_when_budget_cap_exceeded(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        # Force a non-zero per-call cost so the cap can be tripped.
        from epublate.llm.pricing import ModelPrice, set_price

        set_price(
            "gpt-mock",
            ModelPrice(input_per_mtok=1000.0, output_per_mtok=1000.0),
        )

        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())

        # Budget so small that one call should already exceed it.
        with pytest.raises(BatchPaused) as exc_info:
            run_batch(
                engine=project.engine,
                project_id=project.project_id,
                source_lang=project.source_lang,
                target_lang=project.target_lang,
                provider=provider,
                options=BatchOptions(
                    model="gpt-mock",
                    budget_usd=0.0001,
                    concurrency=1,
                ),
            )

        # The pause has a populated summary attached.
        summary = exc_info.value.summary
        assert summary.attempted >= 1
        assert summary.cost_usd > 0.0001
        assert summary.paused_reason is not None

        events = repo.list_events(project.engine, project.project_id)
        kinds = {e.kind for e in events}
        assert "batch.paused" in kinds
        assert "batch.completed" not in kinds

        # Pending segments should remain since the batch stopped early.
        remaining = pending_segments(project.engine, project_id=project.project_id)
        assert remaining
    finally:
        from epublate.llm.pricing import reset_prices

        reset_prices()
        project.close()


def test_run_batch_can_be_cancelled_via_event(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Setting ``cancel_event`` before submission stops the batch fast.

    We pre-set the event so the orchestrator never primes the worker
    pool — that way the assertion is deterministic across CI machines
    (no race on whether the first future completes before the cancel
    flag is observed).
    """

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())

        cancel_event = threading.Event()
        cancel_event.set()

        with pytest.raises(BatchCancelled) as exc_info:
            run_batch(
                engine=project.engine,
                project_id=project.project_id,
                source_lang=project.source_lang,
                target_lang=project.target_lang,
                provider=provider,
                options=BatchOptions(model="gpt-mock"),
                cancel_event=cancel_event,
            )

        summary = exc_info.value.summary
        # Nothing should have actually translated.
        assert summary.translated == 0
        assert summary.attempted == 0

        # The batch.cancelled event landed in the audit log; no
        # batch.completed is emitted on this path so the activity
        # panel can label the run accurately.
        events = repo.list_events(project.engine, project.project_id)
        kinds = {e.kind for e in events}
        assert "batch.cancelled" in kinds
        assert "batch.completed" not in kinds
    finally:
        project.close()


def test_run_batch_cancel_after_first_completion_stops_remaining(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Cancelling mid-flight drains in-flight work and stops new submissions.

    Mirrors the dashboard's "Cancel" button: the curator may hit it
    after a few segments have already gone through. We model that by
    flipping the event the moment the first responder returns; the
    runner must finish the in-flight one and then refuse to submit
    the rest.
    """

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        cancel_event = threading.Event()

        def _responder(messages: list[object], _model: str) -> str:
            last = messages[-1]
            content = getattr(last, "content", "")
            cancel_event.set()
            return json.dumps(
                {
                    "target": f"PT::{content}",
                    "used_entries": [],
                    "new_entities": [],
                }
            )

        provider = MockLLMProvider()
        provider.set_responder(_responder)

        with pytest.raises(BatchCancelled) as exc_info:
            run_batch(
                engine=project.engine,
                project_id=project.project_id,
                source_lang=project.source_lang,
                target_lang=project.target_lang,
                provider=provider,
                options=BatchOptions(
                    model="gpt-mock",
                    concurrency=1,
                    # Force per-segment dispatch so this test asserts on
                    # individual segment progress rather than the
                    # short-segment grouping batch path (which would
                    # finish multiple segments in one LLM call).
                    group_small_segments=False,
                ),
                cancel_event=cancel_event,
            )

        # We let the in-flight task finish (concurrency=1, so just one)
        # and then bailed; the remaining pending segments stay pending.
        summary = exc_info.value.summary
        assert summary.attempted == 1
        assert summary.translated == 1
        remaining = pending_segments(project.engine, project_id=project.project_id)
        assert remaining
    finally:
        project.close()


def test_run_batch_failure_does_not_abort_batch(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        provider = MockLLMProvider()
        # First call returns garbage (causes a parse error inside the
        # pipeline → LLMResponseError); subsequent calls succeed.
        responses = ["this is not even close to JSON"] + [
            json.dumps({"target": "OK"}) for _ in range(20)
        ]
        provider.queue_responses(responses)

        summary = run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(model="gpt-mock"),
        )

        assert summary.failed >= 1
        assert summary.translated >= 1

        events = repo.list_events(project.engine, project.project_id)
        kinds = [e.kind for e in events]
        assert "batch.segment_failed" in kinds
        assert "batch.completed" in kinds
    finally:
        project.close()


def test_run_batch_no_pending_returns_empty_summary(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path, multi=False)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())

        # Translate everything first.
        run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(model="gpt-mock"),
        )

        # Second batch should be a no-op.
        provider.reset()
        summary = run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(model="gpt-mock"),
        )
        assert summary.attempted == 0
        assert provider.call_count == 0
    finally:
        project.close()


def test_run_batch_records_summary_in_batch_completed_event(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path, multi=False)
    try:
        provider = MockLLMProvider()
        provider.set_responder(_placeholder_safe_responder())

        run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(model="gpt-mock"),
        )

        completed = [
            e
            for e in repo.list_events(project.engine, project.project_id)
            if e.kind == "batch.completed"
        ]
        assert completed, "expected a batch.completed event"
        payload = completed[-1].payload
        assert "translated" in payload
        assert "cost_usd" in payload
        assert "elapsed_s" in payload
    finally:
        project.close()


def test_run_batch_unknown_provider_chat_raises_lifts_to_failure(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    # The unconfigured MockLLMProvider raises LLMResponseError on every
    # chat() — the batch must catch each one, record a failure, and not
    # crash the worker pool.
    assert LLMResponseError is not None

    project = _make_project(tiny_epub_factory, tmp_path, multi=False)
    try:
        provider = MockLLMProvider()

        summary = run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(model="gpt-mock"),
        )
        assert summary.failed == summary.attempted
        assert summary.translated == 0
    finally:
        project.close()


def test_run_batch_pre_pass_seeds_glossary_before_translating(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """The M5 pre-pass must surface candidates *before* the translator runs.

    Routes responses by message-content shape: extractor calls have an
    ``Existing glossary`` system prompt, translator calls have the
    ``[[T0]]`` placeholder rules. Asserts that:
      * the helper LLM proposed a fresh glossary entry,
      * the batch translated normally,
      * the ``batch.pre_pass_completed`` event landed.
    """

    project = _make_project(tiny_epub_factory, tmp_path, multi=True)
    try:
        provider = MockLLMProvider()

        def _responder(messages: list[object], _model: str) -> str:
            system = getattr(messages[0], "content", "")
            user = getattr(messages[-1], "content", "")
            if "Existing glossary" in system:
                return json.dumps(
                    {
                        "entities": [
                            {"type": "character", "source": "FreshName"},
                        ],
                        "pov": "third_limited",
                    }
                )
            return json.dumps(
                {
                    "target": f"PT::{user}",
                    "used_entries": [],
                    "new_entities": [],
                }
            )

        provider.set_responder(_responder)

        summary = run_batch(
            engine=project.engine,
            project_id=project.project_id,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            provider=provider,
            options=BatchOptions(
                model="gpt-mock",
                pre_pass=True,
                helper_model="gpt-mock-helper",
            ),
        )

        assert summary.pre_pass is not None
        assert summary.pre_pass.chunks >= 1
        assert summary.pre_pass.proposed_count >= 1
        # Translation still happened normally.
        assert summary.translated > 0
        assert summary.failed == 0

        # Helper-model audit row uses the ``extract`` purpose so it doesn't
        # mix with translator rows.
        calls = repo.list_llm_calls(project.engine, project.project_id)
        assert any(c.purpose == "extract" for c in calls)
        assert any(c.purpose == "translate" for c in calls)

        events = [e.kind for e in repo.list_events(project.engine, project.project_id)]
        assert "batch.pre_pass_started" in events
        assert "batch.pre_pass_completed" in events

        proposed = repo.list_glossary_entries(
            project.engine, project.project_id, status="proposed"
        )
        assert any(p.source_term == "FreshName" for p in proposed)
    finally:
        project.close()
