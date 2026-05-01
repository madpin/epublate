"""Batch translation runner (PRD §7.3, §4.4 / M4).

The batch runner is the headless equivalent of the Reader's
translate-next loop: it walks a project's pending segments, calls
:func:`epublate.core.pipeline.translate_segment` on each, and aggregates
the per-segment outcomes into a :class:`BatchSummary` the curator (or
the Inbox screen) can act on.

Hard rules from the PRD:

* **Concurrency cap.** Defaults to 1; user-configurable. A
  :class:`concurrent.futures.ThreadPoolExecutor` parallelizes calls.
  SQLite + WAL serializes commits, so multi-thread writes from
  ``translate_segment`` are safe (the per-segment transaction is short).
* **Budget cap.** When the cumulative spend would cross
  ``options.budget_usd`` we stop submitting new tasks, drain the
  in-flight ones, append a ``batch.paused`` event, and raise
  :class:`BatchPaused` so the caller can resume after the curator
  raises the cap (PRD F-LLM-8).
* **Failure isolation.** Per-segment failures (parse errors, validator
  rejects, transport errors past the LLM provider's retry budget) are
  caught at the worker boundary, recorded as ``batch.segment_failed``
  events, and **do not abort** the batch (PRD §7.3 step 3).

Cache hits cost zero, so they don't count against the budget; this
matches the Reader's accounting model and lets a curator re-run a
batch on a populated cache without paying a second time.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from sqlalchemy.engine import Engine

from epublate.core.extractor import (
    DEFAULT_CHUNK_MAX_TOKENS,
    IntakeOptions,
    IntakeSummary,
    run_pre_pass,
)
from epublate.core.pipeline import (
    GROUP_DEFAULT_MAX_ITEMS,
    GROUP_DEFAULT_MAX_SOURCE_CHARS,
    TranslateOptions,
    TranslateOutcome,
    is_group_eligible,
    translate_segment,
    translate_segments_grouped,
)
from epublate.db import repo
from epublate.errors import EpublateError
from epublate.llm.base import LLMProvider

_logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class BatchOptions:
    """Knobs for one batch run.

    ``concurrency`` defaults to 1 per PRD F-LLM-5; raising it past the
    LLM provider's rate limit is the user's choice. ``budget_usd``
    overrides the project's stored budget for this single run; when
    ``None``, the project's row is consulted. ``chapter_ids`` narrows
    the scope; ``None`` means every pending segment in the project.
    ``bypass_cache`` flips the pipeline's cache-busting salt for the
    "re-run from scratch" workflow.

    ``pre_pass`` opts into the M5 helper-LLM pre-pass: before the
    translator futures fire for each chapter, the helper extractor
    scans the chapter's pending segments for new proper-noun
    candidates and upserts them as ``proposed`` glossary entries so
    the translator's prompt sees them immediately
    (PRD §4.2 phase 3 / M5). ``helper_model`` defaults to ``model``
    when unset (PRD F-LLM-2 explicitly allows reuse).
    ``pre_pass_chunk_max_tokens`` matches the extractor's default and
    can be overridden for endpoints with a tight context window.
    """

    model: str
    concurrency: int = 1
    budget_usd: float | None = None
    chapter_ids: tuple[str, ...] | None = None
    bypass_cache: bool = False
    auto_propose: bool = True
    style_guide: str | None = None
    pre_pass: bool = False
    helper_model: str | None = None
    pre_pass_chunk_max_tokens: int = DEFAULT_CHUNK_MAX_TOKENS
    # Small-segment grouping (PRD F-LLM-5 / PRD §7.3) — collapse dense
    # list-like content (TOCs, indices, glossary pages) into a single
    # LLM round-trip to amortize cost and latency. Keep the default
    # conservative; reliability of the JSON response degrades faster
    # than linearly as the group grows, and a parse failure falls
    # back per-segment (so the maximum win is bounded by this cap).
    group_small_segments: bool = True
    group_max_items: int = GROUP_DEFAULT_MAX_ITEMS
    group_max_source_chars: int = GROUP_DEFAULT_MAX_SOURCE_CHARS


@dataclass(slots=True)
class BatchSummary:
    """Aggregate result of one :func:`run_batch` call.

    ``pre_pass`` carries the helper-LLM pre-pass roll-up when
    ``BatchOptions.pre_pass`` is enabled (PRD §4.2 phase 3 / M5);
    its ``cost_usd`` is *folded into* :attr:`cost_usd` so the budget
    cap math stays correct regardless of which model paid.
    """

    translated: int = 0
    cached: int = 0
    flagged: int = 0
    failed: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    elapsed_s: float = 0.0
    paused_reason: str | None = None
    failures: list[tuple[str, str]] = field(default_factory=list)
    pre_pass: IntakeSummary | None = None

    @property
    def attempted(self) -> int:
        return self.translated + self.cached + self.flagged + self.failed

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(slots=True, frozen=True)
class BatchProgressEvent:
    """One per-segment progress tick the orchestrator hands to its callback.

    The callback is invoked from the worker thread that just finished
    a segment; UI consumers must marshal back to the Textual main loop
    via ``post_message`` (the Dashboard does this).
    """

    segment_id: str
    chapter_id: str
    outcome: TranslateOutcome | None
    error: str | None
    summary: BatchSummary


ProgressCallback = Callable[[BatchProgressEvent], None]


class BatchPaused(EpublateError):
    """Batch stopped because the cumulative cost would cross the budget cap.

    The :attr:`summary` carries the partial results so the caller (CLI
    or Dashboard) can render them and prompt for confirmation before
    resuming with a higher cap.
    """

    def __init__(self, message: str, *, summary: BatchSummary) -> None:
        super().__init__(message)
        self.summary = summary


def run_batch(
    *,
    engine: Engine,
    project_id: str,
    source_lang: str,
    target_lang: str,
    provider: LLMProvider,
    options: BatchOptions,
    on_progress: ProgressCallback | None = None,
    segments: Sequence[repo.SegmentRow] | None = None,
) -> BatchSummary:
    """Translate every pending segment in scope, returning the summary.

    Caller responsibilities:

    * The project must already exist (:func:`epublate.core.project.Project.create`
      or ``open``).
    * ``provider`` is shared across worker threads; the only providers we
      ship (mock + OpenAI-compatible) are thread-safe in practice (the
      OpenAI SDK's ``Client`` instance is documented as such).

    Behavior:

    * Selects pending segments via :func:`pending_segments`.
    * Submits one Future per segment to a ``ThreadPoolExecutor``.
    * Drains futures as they complete; updates the running
      :class:`BatchSummary` after each.
    * Stops accepting new work once cumulative ``cost_usd`` would
      exceed ``effective_budget`` (project budget if any, overridden
      by ``options.budget_usd``); raises :class:`BatchPaused` after
      draining in-flight tasks.
    * Per-segment failures don't abort: they're recorded on the
      summary and emitted as ``batch.segment_failed`` events.
    """

    project_row = repo.get_project(engine, project_id)
    if project_row is None:
        raise EpublateError(f"project not found: {project_id}")

    effective_budget = options.budget_usd
    if effective_budget is None:
        effective_budget = project_row.budget_usd

    # Mirror the budget-fallback pattern: when the caller didn't pin a
    # style explicitly we use whatever is on the project row (PRD
    # F-STYLE-1 / F-STYLE-2). This is what lets the Dashboard / Reader
    # pass a bare ``BatchOptions`` and still get the curator's chosen
    # tone in every translator call.
    effective_style_guide = options.style_guide
    if effective_style_guide is None:
        effective_style_guide = project_row.style_guide

    pending = (
        list(segments)
        if segments is not None
        else _select_pending(
            engine, project_id=project_id, chapter_ids=options.chapter_ids
        )
    )

    summary = BatchSummary()
    started = time.monotonic()
    repo.append_event(
        engine,
        project_id=project_id,
        kind="batch.started",
        payload={
            "model": options.model,
            "concurrency": options.concurrency,
            "budget_usd": effective_budget,
            "segment_count": len(pending),
            "chapter_ids": list(options.chapter_ids) if options.chapter_ids else None,
            "pre_pass": options.pre_pass,
        },
    )

    if not pending:
        summary.elapsed_s = time.monotonic() - started
        repo.append_event(
            engine,
            project_id=project_id,
            kind="batch.completed",
            payload=_summary_payload(summary),
        )
        return summary

    if options.pre_pass:
        helper_model = options.helper_model or options.model
        pre_options = IntakeOptions(
            model=helper_model,
            chunk_max_tokens=options.pre_pass_chunk_max_tokens,
            bypass_cache=options.bypass_cache,
            auto_propose=options.auto_propose,
        )
        rolled = IntakeSummary()
        # Walk pending segments grouped by chapter so each helper call sees
        # one chapter's worth of context at a time (PRD §4.2 phase 3).
        for chapter_id, chapter_segments in _group_by_chapter(pending):
            chapter_summary = run_pre_pass(
                engine=engine,
                project_id=project_id,
                source_lang=source_lang,
                target_lang=target_lang,
                provider=provider,
                options=pre_options,
                segments=chapter_segments,
            )
            del chapter_id  # used only for grouping order
            rolled.chunks += chapter_summary.chunks
            rolled.cached_chunks += chapter_summary.cached_chunks
            rolled.proposed_count += chapter_summary.proposed_count
            rolled.prompt_tokens += chapter_summary.prompt_tokens
            rolled.completion_tokens += chapter_summary.completion_tokens
            rolled.cost_usd += chapter_summary.cost_usd
            rolled.failed_chunks += chapter_summary.failed_chunks
            rolled.proposed_entry_ids.extend(chapter_summary.proposed_entry_ids)
            if chapter_summary.pov and rolled.pov is None:
                rolled.pov = chapter_summary.pov
            if chapter_summary.tense and rolled.tense is None:
                rolled.tense = chapter_summary.tense
            if chapter_summary.notes:
                rolled.notes.extend(chapter_summary.notes)

        summary.pre_pass = rolled
        # Helper spend counts against the same budget cap (PRD F-LLM-8 /
        # F-LLM-7). Tokens are folded so the overall accounting matches
        # what shows up in the audit table.
        summary.prompt_tokens += rolled.prompt_tokens
        summary.completion_tokens += rolled.completion_tokens
        summary.cost_usd += rolled.cost_usd

    concurrency = max(1, options.concurrency)
    paused = False
    pause_reason: str | None = None

    # Lock guards every mutation of ``summary`` since Futures complete on
    # worker threads. Cheap: we only hold it for a few field updates.
    lock = threading.Lock()

    # Each work item is either a single segment or a group of
    # eligible short segments translated in one LLM round-trip.
    #
    # ``WorkerResult`` mirrors the input order of the work item so the
    # dispatcher can fan out progress events per input segment even when
    # the worker made a single grouped call.
    WorkerResult = list[tuple[repo.SegmentRow, TranslateOutcome | None, str | None]]

    def _worker_single(seg: repo.SegmentRow) -> WorkerResult:
        try:
            outcome = translate_segment(
                engine=engine,
                project_id=project_id,
                source_lang=source_lang,
                target_lang=target_lang,
                style_guide=effective_style_guide,
                segment=seg,
                provider=provider,
                options=TranslateOptions(
                    model=options.model,
                    bypass_cache=options.bypass_cache,
                    auto_propose=options.auto_propose,
                ),
            )
            return [(seg, outcome, None)]
        except Exception as exc:  # worker boundary; surface, don't crash batch
            _logger.warning(
                "batch worker failed on segment %s: %s",
                seg.id,
                exc,
            )
            return [(seg, None, str(exc))]

    def _worker_group(segs: list[repo.SegmentRow]) -> WorkerResult:
        try:
            outcomes = translate_segments_grouped(
                engine=engine,
                project_id=project_id,
                source_lang=source_lang,
                target_lang=target_lang,
                style_guide=effective_style_guide,
                segments=segs,
                provider=provider,
                options=TranslateOptions(
                    model=options.model,
                    bypass_cache=options.bypass_cache,
                    auto_propose=options.auto_propose,
                ),
            )
            return [
                (seg, outcome, None)
                for seg, outcome in zip(segs, outcomes, strict=True)
            ]
        except Exception as exc:  # worker boundary; surface, don't crash batch
            _logger.warning(
                "batch group worker failed on %d segments: %s",
                len(segs),
                exc,
            )
            err = str(exc)
            return [(seg, None, err) for seg in segs]

    work_items = _build_work_items(pending, options=options)

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        in_flight: set[Future[WorkerResult]] = set()
        queue: list[list[repo.SegmentRow]] = list(work_items)
        # Prime the pool with up to ``concurrency`` tasks. After that
        # we submit one new task per completion (sliding window) so the
        # budget cap can short-circuit further submissions cleanly.
        while queue and len(in_flight) < concurrency:
            in_flight.add(
                _submit_work_item(pool, queue.pop(0), _worker_single, _worker_group)
            )

        while in_flight:
            done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
            for fut in done:
                in_flight.discard(fut)
                results = fut.result()
                for seg, outcome, err in results:
                    with lock:
                        _apply_outcome(summary, outcome, err)
                        over_budget = (
                            effective_budget is not None
                            and summary.cost_usd > effective_budget
                        )
                        snapshot = BatchSummary(
                            translated=summary.translated,
                            cached=summary.cached,
                            flagged=summary.flagged,
                            failed=summary.failed,
                            prompt_tokens=summary.prompt_tokens,
                            completion_tokens=summary.completion_tokens,
                            cost_usd=summary.cost_usd,
                            elapsed_s=time.monotonic() - started,
                            paused_reason=summary.paused_reason,
                            failures=list(summary.failures),
                        )
                    if err is not None:
                        repo.append_event(
                            engine,
                            project_id=project_id,
                            kind="batch.segment_failed",
                            payload={
                                "segment_id": seg.id,
                                "chapter_id": seg.chapter_id,
                                "error": err,
                            },
                        )
                    if on_progress is not None:
                        on_progress(
                            BatchProgressEvent(
                                segment_id=seg.id,
                                chapter_id=seg.chapter_id,
                                outcome=outcome,
                                error=err,
                                summary=snapshot,
                            )
                        )
                    if over_budget and not paused:
                        paused = True
                        pause_reason = (
                            f"budget cap ${effective_budget:.4f} reached "
                            f"at ${summary.cost_usd:.4f}"
                        )
                        queue.clear()  # stop submitting new work
            # Top up the pool only if we haven't paused.
            while not paused and queue and len(in_flight) < concurrency:
                in_flight.add(
                    _submit_work_item(pool, queue.pop(0), _worker_single, _worker_group)
                )

    summary.elapsed_s = time.monotonic() - started

    if paused:
        summary.paused_reason = pause_reason
        repo.append_event(
            engine,
            project_id=project_id,
            kind="batch.paused",
            payload={
                **_summary_payload(summary),
                "reason": pause_reason,
                "budget_usd": effective_budget,
            },
        )
        raise BatchPaused(pause_reason or "batch paused", summary=summary)

    repo.append_event(
        engine,
        project_id=project_id,
        kind="batch.completed",
        payload=_summary_payload(summary),
    )
    return summary


def _apply_outcome(
    summary: BatchSummary,
    outcome: TranslateOutcome | None,
    err: str | None,
) -> None:
    """Fold one worker result into the running summary.

    Caller holds ``summary``'s lock. Cache hits accumulate token + cost
    counters at zero (the pipeline's :class:`TranslateOutcome` already
    reports ``cost_usd=0``) so the budget cap math stays correct.
    """

    if err is not None:
        summary.failed += 1
        seg_id = err if len(err) <= 80 else err[:80] + "…"
        summary.failures.append(("error", seg_id))
        return
    assert outcome is not None
    summary.prompt_tokens += outcome.prompt_tokens
    summary.completion_tokens += outcome.completion_tokens
    summary.cost_usd += outcome.cost_usd
    if outcome.flagged:
        summary.flagged += 1
    elif outcome.cache_hit:
        summary.cached += 1
    else:
        summary.translated += 1


def _summary_payload(summary: BatchSummary) -> dict[str, object]:
    payload: dict[str, object] = {
        "translated": summary.translated,
        "cached": summary.cached,
        "flagged": summary.flagged,
        "failed": summary.failed,
        "prompt_tokens": summary.prompt_tokens,
        "completion_tokens": summary.completion_tokens,
        "cost_usd": summary.cost_usd,
        "elapsed_s": summary.elapsed_s,
    }
    if summary.pre_pass is not None:
        payload["pre_pass"] = {
            "chunks": summary.pre_pass.chunks,
            "cached_chunks": summary.pre_pass.cached_chunks,
            "proposed_count": summary.pre_pass.proposed_count,
            "cost_usd": summary.pre_pass.cost_usd,
            "failed_chunks": summary.pre_pass.failed_chunks,
        }
    return payload


def _build_work_items(
    pending: Sequence[repo.SegmentRow],
    *,
    options: BatchOptions,
) -> list[list[repo.SegmentRow]]:
    """Partition ``pending`` into work items the worker pool can fan out.

    A work item is either ``[one_segment]`` (per-segment call) or a
    group of up to ``options.group_max_items`` eligible segments that
    share a chapter. Grouping never crosses a chapter boundary: keeping
    each group to one chapter (a) lets failure modes stay local to a
    single chapter and (b) means each grouped call shares the same
    narrative context.

    When ``options.group_small_segments`` is False we emit one work
    item per pending segment, matching the pre-grouping behavior
    exactly (useful for debugging or for providers that don't play
    nicely with JSON-list responses).
    """

    if not options.group_small_segments or options.group_max_items <= 1:
        return [[seg] for seg in pending]

    items: list[list[repo.SegmentRow]] = []
    # Walk in the caller's order so the progress events still land in
    # reading order (chapter-major, segment-minor). A short run of
    # eligible segments inside one chapter becomes a single group;
    # anything ineligible (or a chapter boundary) flushes the run.
    current_chapter: str | None = None
    buffer: list[repo.SegmentRow] = []

    def _flush() -> None:
        if buffer:
            items.append(list(buffer))
            buffer.clear()

    for seg in pending:
        eligible = is_group_eligible(
            seg, max_source_chars=options.group_max_source_chars
        )
        if not eligible:
            _flush()
            items.append([seg])
            current_chapter = None
            continue
        if seg.chapter_id != current_chapter:
            _flush()
            current_chapter = seg.chapter_id
        buffer.append(seg)
        if len(buffer) >= options.group_max_items:
            _flush()
            current_chapter = None
    _flush()

    # Collapse trivial 1-item "groups" back into singletons so the worker
    # dispatcher picks the (cheaper) per-segment path for them.
    result: list[list[repo.SegmentRow]] = []
    for item in items:
        if len(item) == 1:
            result.append(item)
        else:
            result.append(item)
    return result


def _submit_work_item(
    pool: ThreadPoolExecutor,
    item: list[repo.SegmentRow],
    worker_single: Callable[
        [repo.SegmentRow],
        list[tuple[repo.SegmentRow, TranslateOutcome | None, str | None]],
    ],
    worker_group: Callable[
        [list[repo.SegmentRow]],
        list[tuple[repo.SegmentRow, TranslateOutcome | None, str | None]],
    ],
) -> Future[list[tuple[repo.SegmentRow, TranslateOutcome | None, str | None]]]:
    """Submit ``item`` to ``pool`` via the matching worker variant."""

    if len(item) == 1:
        return pool.submit(worker_single, item[0])
    return pool.submit(worker_group, item)


def _group_by_chapter(
    segments: Iterable[repo.SegmentRow],
) -> Iterable[tuple[str, list[repo.SegmentRow]]]:
    """Yield ``(chapter_id, segments)`` runs preserving input order.

    The pending list arrives sorted by spine_idx then segment idx (the
    repo helper that produced it joins through ``chapter`` and orders
    accordingly), so a simple itertools-style group-by gives us natural
    chapter buckets without re-sorting.
    """

    current_id: str | None = None
    current_bucket: list[repo.SegmentRow] = []
    for seg in segments:
        if seg.chapter_id != current_id:
            if current_bucket and current_id is not None:
                yield current_id, current_bucket
            current_id = seg.chapter_id
            current_bucket = []
        current_bucket.append(seg)
    if current_bucket and current_id is not None:
        yield current_id, current_bucket


def _select_pending(
    engine: Engine,
    *,
    project_id: str,
    chapter_ids: Iterable[str] | None,
) -> list[repo.SegmentRow]:
    """Read pending segments straight off the project, optionally narrowed.

    Imported lazily-via-helper to keep the dependency graph simple
    (``core.batch`` should only know about ``db.repo``, not the future
    ``core.stats`` module that exposes the same idea for the Dashboard).
    """

    return repo.list_segments_by_status(
        engine,
        project_id=project_id,
        status="pending",
        chapter_ids=tuple(chapter_ids) if chapter_ids is not None else None,
    )


__all__ = [
    "BatchOptions",
    "BatchPaused",
    "BatchProgressEvent",
    "BatchSummary",
    "ProgressCallback",
    "run_batch",
]
