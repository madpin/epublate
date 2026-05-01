"""Project stats + alert aggregator (PRD F-T-2 / M4).

Read-only helpers that the Dashboard, Inbox, and CLI ``stats`` command
consume. They roll up:

* ``llm_call`` for spend, token counts, cache hit rate, spend-by-model.
* ``segment`` for status histogram (translated / approved / flagged / …).
* ``event`` for recent curator-relevant alerts (batch finished /
  paused, locked-glossary violations, freshly proposed entries).

Everything here is side-effect free; no writes happen on this path so
the M4 Dashboard can poll cheaply between batch ticks. The aggregations
are deliberately one-shot (no caching) — SQLite + WAL handles a
sub-millisecond ``SELECT count(*)`` on these tables for any plausible
project size.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.engine import Engine

from epublate.db import repo, schema

# Alert kinds the Inbox and Dashboard surface in their "recent activity"
# feed (PRD §4.6). Anything not in this set is either a low-level audit
# trace (``segment.translated``) or already covered by a richer view
# (``glossary.cascaded`` is reflected in the Glossary screen).
ALERT_KINDS: frozenset[str] = frozenset(
    [
        "batch.started",
        "batch.completed",
        "batch.paused",
        "batch.segment_failed",
        "batch.pre_pass_started",
        "batch.pre_pass_completed",
        "segment.translation_flagged",
        "segment.translation_failed",
        "entity.proposed",
        "entity.extracted",
        "entity.extract_failed",
        "intake.started",
        "intake.completed",
        "project.budget_changed",
        "glossary.cascaded",
    ]
)


@dataclass(slots=True, frozen=True)
class ChapterShape:
    """One row in the chapter histogram used by the BatchModal pre-flight.

    ``approved`` and ``translated`` only count translatable segments
    (i.e. those that survived the Reader's
    :func:`epublate.app.preview.has_translatable_text` filter), so the
    pre-flight numbers match the queue the curator actually navigates.
    """

    chapter_id: str
    spine_idx: int
    title: str | None
    href: str
    segment_count: int
    translatable_count: int
    approved_count: int
    translated_count: int
    pending_count: int


@dataclass(slots=True, frozen=True)
class ChapterShapeSummary:
    """Aggregate over :class:`ChapterShape`-s for the BatchModal preview."""

    total_chapters: int
    translatable_chapters: int
    total_segments: int
    longest: ChapterShape | None
    shortest: ChapterShape | None
    average_segments: float
    median_segments: float


@dataclass(slots=True, frozen=True)
class ProjectStats:
    """Snapshot of project-level counters for the Dashboard."""

    project_id: str
    budget_usd: float | None
    spend_usd: float
    prompt_tokens: int
    completion_tokens: int
    cache_hits: int
    llm_calls: int
    cache_hit_rate: float
    chapter_count: int
    segment_count: int
    segments_by_status: dict[str, int] = field(default_factory=dict)
    spend_by_model: dict[str, float] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    @property
    def translated_count(self) -> int:
        """Count of segments past ``pending``: any kind of translation."""

        return sum(
            v
            for k, v in self.segments_by_status.items()
            if k != schema.SegmentStatus.PENDING
        )

    @property
    def approved_count(self) -> int:
        return self.segments_by_status.get(schema.SegmentStatus.APPROVED, 0)

    @property
    def flagged_count(self) -> int:
        return self.segments_by_status.get(schema.SegmentStatus.FLAGGED, 0)

    @property
    def progress_ratio(self) -> float:
        if not self.segment_count:
            return 0.0
        return self.translated_count / self.segment_count

    @property
    def validation_failure_rate(self) -> float:
        """Locked-glossary failures over translation attempts (excl. pending)."""

        attempts = self.translated_count
        if not attempts:
            return 0.0
        return self.flagged_count / attempts

    @property
    def remaining_budget(self) -> float | None:
        if self.budget_usd is None:
            return None
        return max(0.0, self.budget_usd - self.spend_usd)


def compute_stats(engine: Engine, project_id: str) -> ProjectStats:
    """Single-query-per-table aggregation (PRD F-T-2)."""

    project_row = repo.get_project(engine, project_id)
    if project_row is None:
        raise ValueError(f"project not found: {project_id}")

    spend_total = 0.0
    prompt_total = 0
    completion_total = 0
    cache_hits = 0
    llm_calls = 0
    spend_by_model: dict[str, float] = {}

    spend_stmt = (
        select(
            schema.llm_call.c.model,
            func.coalesce(func.sum(schema.llm_call.c.cost_usd), 0.0).label("cost"),
            func.coalesce(func.sum(schema.llm_call.c.prompt_tokens), 0).label("ptok"),
            func.coalesce(func.sum(schema.llm_call.c.completion_tokens), 0).label(
                "ctok"
            ),
            func.count().label("calls"),
            func.coalesce(func.sum(schema.llm_call.c.cache_hit), 0).label("hits"),
        )
        .where(schema.llm_call.c.project_id == project_id)
        .group_by(schema.llm_call.c.model)
    )
    with engine.begin() as conn:
        for row in conn.execute(spend_stmt).mappings():
            cost = float(row["cost"] or 0.0)
            spend_total += cost
            prompt_total += int(row["ptok"] or 0)
            completion_total += int(row["ctok"] or 0)
            llm_calls += int(row["calls"] or 0)
            cache_hits += int(row["hits"] or 0)
            spend_by_model[str(row["model"])] = cost

        status_stmt = (
            select(
                schema.segment.c.status,
                func.count().label("n"),
            )
            .join(
                schema.chapter,
                schema.chapter.c.id == schema.segment.c.chapter_id,
            )
            .where(schema.chapter.c.project_id == project_id)
            .group_by(schema.segment.c.status)
        )
        segments_by_status: dict[str, int] = {}
        for row in conn.execute(status_stmt).mappings():
            segments_by_status[str(row["status"])] = int(row["n"] or 0)

        chapter_count = int(
            conn.execute(
                select(func.count())
                .select_from(schema.chapter)
                .where(schema.chapter.c.project_id == project_id)
            ).scalar()
            or 0
        )

    segment_count = sum(segments_by_status.values())
    cache_hit_rate = (cache_hits / llm_calls) if llm_calls else 0.0

    return ProjectStats(
        project_id=project_id,
        budget_usd=project_row.budget_usd,
        spend_usd=spend_total,
        prompt_tokens=prompt_total,
        completion_tokens=completion_total,
        cache_hits=cache_hits,
        llm_calls=llm_calls,
        cache_hit_rate=cache_hit_rate,
        chapter_count=chapter_count,
        segment_count=segment_count,
        segments_by_status=segments_by_status,
        spend_by_model=spend_by_model,
    )


def compute_spend(engine: Engine, project_id: str) -> float:
    """``sum(llm_call.cost_usd)`` for ``project_id`` (PRD F-LLM-7)."""

    stmt = select(func.coalesce(func.sum(schema.llm_call.c.cost_usd), 0.0)).where(
        schema.llm_call.c.project_id == project_id
    )
    with engine.begin() as conn:
        return float(conn.execute(stmt).scalar() or 0.0)


def pending_segments(
    engine: Engine,
    *,
    project_id: str,
    chapter_ids: Iterable[str] | None = None,
) -> list[repo.SegmentRow]:
    """Return every ``pending`` segment in book order."""

    return repo.list_segments_by_status(
        engine,
        project_id=project_id,
        status=schema.SegmentStatus.PENDING,
        chapter_ids=tuple(chapter_ids) if chapter_ids is not None else None,
    )


def flagged_segments(
    engine: Engine,
    *,
    project_id: str,
) -> list[repo.SegmentRow]:
    """Return every locked-glossary-flagged segment in book order."""

    return repo.list_segments_by_status(
        engine,
        project_id=project_id,
        status=schema.SegmentStatus.FLAGGED,
    )


def compute_chapter_shapes(
    engine: Engine,
    *,
    project_id: str,
) -> list[ChapterShape]:
    """Return one :class:`ChapterShape` per chapter ordered by spine.

    Used by the BatchModal pre-flight to show the curator how the
    project is shaped before they commit to a run; cheap enough to
    call eagerly thanks to the small expected fanout (a couple
    hundred chapters at most).

    Image-only / structurally-empty hosts are filtered out of the
    "translatable" count to match the Reader's navigation queue.
    """

    # Imported locally to avoid pulling the app layer into ``core``
    # — this is the only spot in stats.py that needs the preview filter.
    from epublate.app.preview import has_translatable_text

    chapters = repo.list_chapters(engine, project_id)
    shapes: list[ChapterShape] = []
    for chap in chapters:
        segs = repo.list_segments(engine, chap.id)
        translatable = [s for s in segs if has_translatable_text(s.source_text)]
        approved = sum(
            1 for s in translatable if s.status == schema.SegmentStatus.APPROVED
        )
        translated = sum(
            1
            for s in translatable
            if s.status
            in (schema.SegmentStatus.TRANSLATED, schema.SegmentStatus.APPROVED)
        )
        shapes.append(
            ChapterShape(
                chapter_id=chap.id,
                spine_idx=chap.spine_idx,
                title=chap.title,
                href=chap.href,
                segment_count=len(segs),
                translatable_count=len(translatable),
                approved_count=approved,
                translated_count=translated,
                pending_count=len(translatable) - translated,
            )
        )
    return shapes


def summarize_chapter_shapes(
    shapes: Iterable[ChapterShape],
) -> ChapterShapeSummary:
    """Aggregate :class:`ChapterShape` rows for the BatchModal preview."""

    rows = [s for s in shapes if s.translatable_count > 0]
    if not rows:
        return ChapterShapeSummary(
            total_chapters=sum(1 for _ in shapes) or len(rows),
            translatable_chapters=0,
            total_segments=0,
            longest=None,
            shortest=None,
            average_segments=0.0,
            median_segments=0.0,
        )
    counts = sorted(r.translatable_count for r in rows)
    total_segments = sum(counts)
    n = len(counts)
    if n % 2:
        median = float(counts[n // 2])
    else:
        median = (counts[n // 2 - 1] + counts[n // 2]) / 2.0
    longest = max(rows, key=lambda r: r.translatable_count)
    shortest = min(rows, key=lambda r: r.translatable_count)
    # ``total_chapters`` reflects every chapter (front matter, nav,
    # blank dividers, …) so the curator sees the file count too.
    total_chapters = max(len(rows), max((s.spine_idx for s in shapes), default=0) + 1)
    return ChapterShapeSummary(
        total_chapters=total_chapters,
        translatable_chapters=n,
        total_segments=total_segments,
        longest=longest,
        shortest=shortest,
        average_segments=total_segments / n,
        median_segments=median,
    )


def recent_alerts(
    engine: Engine,
    *,
    project_id: str,
    limit: int = 20,
    kinds: Iterable[str] | None = None,
) -> list[repo.EventRow]:
    """Newest-first slice of curator-relevant events for the Inbox.

    ``kinds`` defaults to :data:`ALERT_KINDS`; pass an explicit set to
    narrow further (e.g. just ``{"batch.paused"}`` for the budget-cap
    feed). Returns at most ``limit`` rows in descending order.
    """

    allowed = frozenset(kinds) if kinds is not None else ALERT_KINDS
    stmt = (
        select(schema.event)
        .where(schema.event.c.project_id == project_id)
        .where(schema.event.c.kind.in_(tuple(allowed)))
        .order_by(schema.event.c.id.desc())
        .limit(limit)
    )
    import json

    with engine.begin() as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        repo.EventRow(
            id=int(r["id"]),
            project_id=str(r["project_id"]),
            ts=int(r["ts"]),
            kind=str(r["kind"]),
            payload=json.loads(r["payload_json"]) if r["payload_json"] else {},
        )
        for r in rows
    ]


__all__ = [
    "ALERT_KINDS",
    "ChapterShape",
    "ChapterShapeSummary",
    "ProjectStats",
    "compute_chapter_shapes",
    "compute_spend",
    "compute_stats",
    "flagged_segments",
    "pending_segments",
    "recent_alerts",
    "summarize_chapter_shapes",
]
