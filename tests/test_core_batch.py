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
    BatchPrePassProgress,
    BatchProgressEvent,
    run_batch,
)
from epublate.core.project import Project
from epublate.core.stats import pending_segments
from epublate.db import repo
from epublate.db.schema import SegmentStatus
from epublate.errors import LLMRateLimitError, LLMResponseError
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


def test_run_batch_pre_pass_interleaves_with_translation_per_chapter(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """First chapter's translation runs *before* second chapter's pre-pass.

    Reproduces the bug that motivated the per-chapter interleave: with
    the old all-pre-pass-first ordering the translator pool didn't
    start until every chapter's helper extractor had finished, so on a
    slow helper the meter sat at ``0 / N`` for minutes and looked like
    a deadlock. Records the *order* of LLM calls (extract vs translate)
    and asserts at least one ``translate`` call lands before the second
    ``extract`` call (the chapter-2 pre-pass).
    """

    project = _make_project(tiny_epub_factory, tmp_path, multi=True)
    try:
        provider = MockLLMProvider()

        call_order: list[tuple[str, str]] = []

        def _responder(messages: list[object], _model: str) -> str:
            system = getattr(messages[0], "content", "")
            user = getattr(messages[-1], "content", "")
            if "Existing glossary" in system:
                call_order.append(("extract", user[:24]))
                return json.dumps(
                    {
                        "entities": [{"type": "character", "source": "Alice"}],
                        "pov": "third_limited",
                    }
                )
            call_order.append(("translate", user[:24]))
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

        assert summary.failed == 0
        assert summary.translated > 0
        assert summary.pre_pass is not None
        assert summary.pre_pass.chunks >= 2  # two chapters → two pre-passes

        # Find indices of the two extract calls and the first translate.
        extract_indices = [i for i, (k, _) in enumerate(call_order) if k == "extract"]
        translate_indices = [
            i for i, (k, _) in enumerate(call_order) if k == "translate"
        ]
        assert len(extract_indices) >= 2, "expected one pre-pass call per chapter"
        assert translate_indices, "expected at least one translation"
        # A translate call must appear between the first and second extract:
        # i.e. chapter 1's segments translate before chapter 2's pre-pass runs.
        first_translate = translate_indices[0]
        second_extract = extract_indices[1]
        assert first_translate < second_extract, (
            "expected interleave: chapter 1 translate before chapter 2 pre-pass; "
            f"call_order={call_order}"
        )
    finally:
        project.close()


def test_run_batch_pre_pass_cancels_promptly(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Setting ``cancel_event`` during pre-pass aborts before next chapter.

    Without this fix, ``cancel_event`` was only honored inside the
    translator dispatch loop, so a curator pressing Cancel while the
    helper LLM was running had to wait until *every* chapter's pre-pass
    completed before translation even started — and Cancel did nothing
    in the meantime.
    """

    project = _make_project(tiny_epub_factory, tmp_path, multi=True)
    try:
        provider = MockLLMProvider()
        cancel_event = threading.Event()

        def _responder(messages: list[object], _model: str) -> str:
            system = getattr(messages[0], "content", "")
            user = getattr(messages[-1], "content", "")
            if "Existing glossary" in system:
                # Trip cancel inside the first chunk's call so the
                # next chunk / chapter trips out of the loop.
                cancel_event.set()
                return json.dumps(
                    {
                        "entities": [{"type": "character", "source": "Alice"}],
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

        with pytest.raises(BatchCancelled):
            run_batch(
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
                cancel_event=cancel_event,
            )

        events = [e.kind for e in repo.list_events(project.engine, project.project_id)]
        assert "batch.cancelled" in events
        assert "batch.completed" not in events
        # The first chapter's pre-pass either completed or was cancelled
        # before chapter 2 ran; either way no translation is required to
        # land for the cancel to take effect.
    finally:
        project.close()


def test_run_batch_pre_pass_budget_cap_pauses_before_translation(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A tiny budget cap pauses the run inside the helper loop.

    Without folding helper spend into ``summary.cost_usd`` *between*
    chapters, a runaway helper could blow through the budget cap and
    the pause check wouldn't fire until the (much later) translator
    pool started. The cap-while-pre-pass test pins this down so the
    interleave can't regress.
    """

    from epublate.llm.pricing import ModelPrice, reset_prices, set_price

    project = _make_project(tiny_epub_factory, tmp_path, multi=True)
    try:
        # Force a non-zero per-call cost on the helper so the cap can trip.
        set_price(
            "gpt-mock-helper",
            ModelPrice(input_per_mtok=1000.0, output_per_mtok=1000.0),
        )
        provider = MockLLMProvider()

        def _responder(messages: list[object], _model: str) -> str:
            system = getattr(messages[0], "content", "")
            user = getattr(messages[-1], "content", "")
            if "Existing glossary" in system:
                return json.dumps(
                    {
                        "entities": [{"type": "character", "source": "Alice"}],
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

        with pytest.raises(BatchPaused) as exc_info:
            run_batch(
                engine=project.engine,
                project_id=project.project_id,
                source_lang=project.source_lang,
                target_lang=project.target_lang,
                provider=provider,
                options=BatchOptions(
                    model="gpt-mock",
                    pre_pass=True,
                    helper_model="gpt-mock-helper",
                    budget_usd=0.0001,
                ),
            )
        summary = exc_info.value.summary
        assert summary.paused_reason is not None
        assert "budget cap" in summary.paused_reason
        # The pause must happen before the (much later) translator phase,
        # i.e. no segments should have been translated when the helper
        # already blew the cap on chapter 1.
        assert summary.translated == 0

        events = [e.kind for e in repo.list_events(project.engine, project.project_id)]
        assert "batch.paused" in events
        assert "batch.completed" not in events
    finally:
        reset_prices()
        project.close()


def test_run_batch_pre_pass_emits_progress_callback(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """``on_pre_pass_progress`` fires once per chunk per chapter.

    The dashboard's "pre-pass: ch X/Y chunk A/B" status line is built
    from these ticks. Without the callback the meter would sit at
    ``0 / N`` for the whole pre-pass, which is the failure mode that
    led to the "translation never starts" bug report.
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
                        "entities": [{"type": "character", "source": "Bob"}],
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
        ticks: list[BatchPrePassProgress] = []

        run_batch(
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
            on_pre_pass_progress=ticks.append,
        )

        assert len(ticks) >= 2, (
            "expected at least one tick per chapter (multi-chapter project)"
        )
        # All ticks should carry the chapter_count for the run.
        chapter_counts = {t.chapter_count for t in ticks}
        assert chapter_counts == {len(set(t.chapter_id for t in ticks))}
        # chapter_index is 1-based and each chunk_index is 0-based but
        # bounded by chunk_count.
        assert all(t.chapter_index >= 1 for t in ticks)
        assert all(0 <= t.chunk_index < t.chunk_count for t in ticks)
        # First chapter's ticks come strictly before the second's.
        order = [t.chapter_index for t in ticks]
        assert order == sorted(order), (
            f"expected per-chapter ordering of pre-pass ticks; got {order}"
        )
    finally:
        project.close()


def test_run_batch_pauses_on_translator_rate_limit(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A 429 from the translator pauses the batch with a clear message.

    Reproduces the OpenRouter free-tier "free-models-per-day" failure
    mode: a depleted daily quota would otherwise return 429 for every
    pending segment in the run, and each was being recorded as an
    opaque per-segment failure. Surfacing as ``BatchPaused`` lets the
    curator switch model / wait / add credits and resume — pending
    segments stay ``pending``.
    """

    project = _make_project(tiny_epub_factory, tmp_path, multi=False)
    try:
        provider = MockLLMProvider()

        # Trip a rate-limit error on the first translator call. Subsequent
        # calls won't fire because the dispatcher pauses on the first
        # rate-limit error.
        def _responder(_messages: list[object], _model: str) -> str:
            raise LLMRateLimitError(
                "OpenAI API status 429: Rate limit exceeded: free-models-per-day. "
                "Add 6.55 credits to unlock 1000 free model requests per day",
                retry_after_seconds=3600.0,
                provider_message=(
                    "Rate limit exceeded: free-models-per-day. Add 6.55 credits "
                    "to unlock 1000 free model requests per day"
                ),
            )

        provider.set_responder(_responder)

        with pytest.raises(BatchPaused) as exc_info:
            run_batch(
                engine=project.engine,
                project_id=project.project_id,
                source_lang=project.source_lang,
                target_lang=project.target_lang,
                provider=provider,
                options=BatchOptions(model="gpt-mock", concurrency=1),
            )

        summary = exc_info.value.summary
        # No segment was recorded as failed — the rate-limit error didn't
        # land in summary.failures, it lifted out as a pause.
        assert summary.failed == 0
        assert summary.translated == 0
        assert summary.paused_reason is not None
        assert "rate limit" in summary.paused_reason.lower()
        assert "free-models-per-day" in summary.paused_reason

        events = [e.kind for e in repo.list_events(project.engine, project.project_id)]
        assert "batch.paused" in events
        assert "batch.completed" not in events
        # Pending segments stay pending so the run is fully resumable
        # once the curator addresses the rate-limit cause.
        remaining = pending_segments(project.engine, project_id=project.project_id)
        assert len(remaining) >= 1
    finally:
        project.close()


def test_run_batch_pauses_on_pre_pass_rate_limit(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """A 429 during the helper pre-pass pauses the run before translation.

    Without this fix the rate-limit error landed inside ``run_pre_pass``,
    got swallowed as a per-chunk failure, the circuit breaker tripped
    after 3 in a row, the chapter aborted, and run_batch moved on to
    the translator phase — which then immediately hit the same 429 for
    every segment. Surfacing the error from run_pre_pass and pausing
    the batch is the right unified behavior.
    """

    project = _make_project(tiny_epub_factory, tmp_path, multi=False)
    try:
        provider = MockLLMProvider()

        # Helper extractor calls have an "Existing glossary" system
        # prompt; trip rate limit on those, succeed elsewhere (though
        # the pause should mean translator never runs).
        def _responder(messages: list[object], _model: str) -> str:
            system = getattr(messages[0], "content", "")
            if "Existing glossary" in system:
                raise LLMRateLimitError(
                    "OpenAI API status 429: helper quota depleted",
                    retry_after_seconds=600.0,
                    provider_message="helper quota depleted",
                )
            return json.dumps(
                {"target": "should not run", "used_entries": [], "new_entities": []}
            )

        provider.set_responder(_responder)

        with pytest.raises(BatchPaused) as exc_info:
            run_batch(
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

        summary = exc_info.value.summary
        # No translation happened — the helper hit the cap first.
        assert summary.translated == 0
        assert summary.paused_reason is not None
        assert "rate limit" in summary.paused_reason.lower()

        events = [e.kind for e in repo.list_events(project.engine, project.project_id)]
        assert "batch.pre_pass_rate_limited" in events
        assert "batch.paused" in events
        assert "batch.completed" not in events
    finally:
        project.close()
