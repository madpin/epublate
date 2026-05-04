"""Repository helpers for the project DB (PRD §6.4).

M0 covered ``project`` + ``event``; M1 adds ``chapter`` + ``segment`` so the
project lifecycle can persist the segmented ePub. Helpers return plain
pydantic models so the TUI / pipeline never holds a Session-bound row
(db-and-persistence rule).
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, func, insert, select, update
from sqlalchemy.engine import Connection, Engine

from epublate.db import schema
from epublate.formats.base import InlineToken, Segment
from epublate.glossary.models import (
    EntityMention,
    EntityType,
    GenderTag,
    GlossaryAlias,
    GlossaryEntry,
    GlossaryEntryWithAliases,
    GlossaryRevision,
    GlossaryStatusLiteral,
)


class ProjectRow(BaseModel):
    """Plain projection of a row in the ``project`` table.

    ``kind`` discriminates regular translation projects (``"book"``)
    from Lore Book projects (``"lore"``). Defaults to ``"book"`` so
    legacy DBs (where the column is implicit) keep behaving identically.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    source_lang: str
    target_lang: str
    source_path: str
    style_guide: str | None = None
    style_profile: str | None = None
    budget_usd: float | None = None
    llm_overrides: str | None = None
    created_at: int
    kind: str = "book"


class EventRow(BaseModel):
    """Plain projection of a row in the append-only ``event`` table."""

    model_config = ConfigDict(extra="forbid")

    id: int | None = None
    project_id: str
    ts: int
    kind: str
    payload: dict[str, Any] = Field(default_factory=dict)


class ChapterRow(BaseModel):
    """Plain projection of a row in the ``chapter`` table."""

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    spine_idx: int
    href: str
    title: str | None = None
    status: str = schema.ChapterStatus.PENDING


class SegmentRow(BaseModel):
    """Plain projection of a row in the ``segment`` table.

    ``inline_skeleton`` mirrors :attr:`Segment.inline_skeleton` (a list of
    :class:`InlineToken`); on disk it is JSON-serialized into the BLOB column.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    chapter_id: str
    idx: int
    source_text: str
    source_hash: str
    target_text: str | None = None
    status: str = schema.SegmentStatus.PENDING
    inline_skeleton: list[InlineToken] = Field(default_factory=list)
    host_path: str = ""
    host_part: int = 0
    host_total_parts: int = 1


class LLMCallRow(BaseModel):
    """Plain projection of a row in the ``llm_call`` table (PRD §6.4).

    Captures the per-call audit trail required by the LLM-integration
    rule: tokens, cost, cache flag, full request/response JSON, and the
    deterministic cache key used to short-circuit future identical calls.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    segment_id: str | None = None
    purpose: str
    model: str
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    cache_hit: bool = False
    cache_key: str | None = None
    request_json: str | None = None
    response_json: str | None = None
    created_at: int = 0


def _now_unix() -> int:
    return int(time.time())


def _new_id() -> str:
    return uuid.uuid4().hex


def _encode_inline_skeleton(seg: SegmentRow) -> bytes:
    """Pack skeleton + host metadata into the ``segment.inline_skeleton`` BLOB.

    The PRD's segment schema only declares ``inline_skeleton`` as opaque
    bytes; we pack the host XPath and split metadata in the same envelope so
    the format-agnostic reassembler can locate the correct DOM node without a
    schema migration.
    """

    envelope = {
        "skeleton": [t.model_dump() for t in seg.inline_skeleton],
        "host_path": seg.host_path,
        "host_part": seg.host_part,
        "host_total_parts": seg.host_total_parts,
    }
    return json.dumps(envelope, ensure_ascii=False).encode("utf-8")


def _decode_inline_skeleton(
    blob: bytes | memoryview | None,
) -> tuple[list[InlineToken], str, int, int]:
    if not blob:
        return [], "", 0, 1
    payload = bytes(blob).decode("utf-8")
    if not payload:
        return [], "", 0, 1
    envelope = json.loads(payload)
    if isinstance(envelope, list):
        # Pre-M1 / external imports might persist just the skeleton list.
        return [InlineToken(**t) for t in envelope], "", 0, 1
    skel = [InlineToken(**t) for t in envelope.get("skeleton", [])]
    return (
        skel,
        str(envelope.get("host_path", "")),
        int(envelope.get("host_part", 0)),
        int(envelope.get("host_total_parts", 1)),
    )


def create_project(
    engine_or_conn: Engine | Connection,
    *,
    name: str,
    source_lang: str,
    target_lang: str,
    source_path: str,
    style_guide: str | None = None,
    style_profile: str | None = None,
    budget_usd: float | None = None,
    project_id: str | None = None,
    created_at: int | None = None,
    kind: str = schema.ProjectKind.BOOK,
) -> ProjectRow:
    row = ProjectRow(
        id=project_id or _new_id(),
        name=name,
        source_lang=source_lang,
        target_lang=target_lang,
        source_path=source_path,
        style_guide=style_guide,
        style_profile=style_profile,
        budget_usd=budget_usd,
        created_at=created_at or _now_unix(),
        kind=kind,
    )
    stmt = insert(schema.project).values(**row.model_dump())
    with _begin(engine_or_conn) as conn:
        conn.execute(stmt)
    return row


def get_project(
    engine_or_conn: Engine | Connection, project_id: str
) -> ProjectRow | None:
    stmt = select(schema.project).where(schema.project.c.id == project_id)
    with _begin(engine_or_conn) as conn:
        result = conn.execute(stmt).mappings().first()
    return _project_row_from_mapping(dict(result)) if result is not None else None


def list_projects(
    engine_or_conn: Engine | Connection,
    *,
    kind: str | None = None,
) -> list[ProjectRow]:
    """List projects in newest-first order, optionally filtered by kind.

    ``kind=None`` returns every row — the historical behaviour, kept
    so callers that don't yet know about Lore Books continue to work.
    Callers that want only translation projects pass
    ``kind=ProjectKind.BOOK``; the LoreBooksScreen passes
    ``kind=ProjectKind.LORE``.
    """

    stmt = select(schema.project).order_by(schema.project.c.created_at.desc())
    if kind is not None:
        stmt = stmt.where(schema.project.c.kind == kind)
    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    return [_project_row_from_mapping(dict(r)) for r in rows]


def _project_row_from_mapping(row: dict[str, Any]) -> ProjectRow:
    """Build a :class:`ProjectRow` while tolerating legacy-shape rows.

    Pre-migration DBs may not have ``kind``; we default it to
    ``"book"`` so an upgrade-in-place doesn't fail in the (brief)
    window between the column being added and the row being backfilled.
    """

    payload = dict(row)
    if "kind" not in payload or payload["kind"] is None:
        payload["kind"] = schema.ProjectKind.BOOK
    return ProjectRow(**payload)


def update_project_style(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    style_profile: str | None,
    style_guide: str | None,
) -> ProjectRow:
    """Set or clear the project's tone preset + resolved prompt block.

    Records a ``project.style_changed`` event in the same transaction so
    the Inbox / activity feed can surface the change. ``style_guide`` is
    what actually lands in the translator's system prompt; ``style_profile``
    is just the slug we show in the UI ("Custom" when ``None``).

    Cache impact: the prompt-block change automatically invalidates
    cached translations because the system prompt hash is part of every
    cache key (PRD F-LLM-6). The caller doesn't need to bypass the
    cache; future translate calls will simply miss until they're warmed.
    """

    with _begin(engine_or_conn) as conn:
        existing = (
            conn.execute(
                select(schema.project).where(schema.project.c.id == project_id)
            )
            .mappings()
            .first()
        )
        if existing is None:
            raise ValueError(f"project not found: {project_id}")
        prev_profile = existing["style_profile"]
        prev_guide = existing["style_guide"]
        conn.execute(
            update(schema.project)
            .where(schema.project.c.id == project_id)
            .values(style_profile=style_profile, style_guide=style_guide)
        )
        append_event(
            conn,
            project_id=project_id,
            kind="project.style_changed",
            payload={
                "prev_profile": (str(prev_profile) if prev_profile else None),
                "new_profile": style_profile,
                "prev_guide_set": prev_guide is not None,
                "new_guide_set": style_guide is not None,
            },
        )
        refreshed = (
            conn.execute(
                select(schema.project).where(schema.project.c.id == project_id)
            )
            .mappings()
            .first()
        )
    assert refreshed is not None
    return ProjectRow(**dict(refreshed))


def get_llm_overrides(
    engine_or_conn: Engine | Connection,
    project_id: str,
) -> dict[str, Any]:
    """Decode the ``project.llm_overrides`` JSON blob to a plain dict.

    Returns an empty dict when the column is ``NULL`` (the common case)
    or when the stored value is not a JSON object — the Settings panel
    falls back to env-var defaults in either case so a corrupted
    override row degrades gracefully.
    """

    row = get_project(engine_or_conn, project_id)
    if row is None or not row.llm_overrides:
        return {}
    try:
        decoded = json.loads(row.llm_overrides)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def set_llm_overrides(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    overrides: dict[str, Any] | None,
) -> ProjectRow:
    """Persist ``overrides`` (or clear them when ``None``).

    Stores the dict as a JSON string so we can grow keys without a
    schema migration. The corresponding ``project.llm_overrides_changed``
    event is appended in the same transaction so the audit log can
    surface model swaps in the activity feed.
    """

    payload: str | None
    if overrides is None or not overrides:
        payload = None
    else:
        payload = json.dumps(overrides, sort_keys=True, ensure_ascii=False)

    with _begin(engine_or_conn) as conn:
        existing = (
            conn.execute(
                select(schema.project).where(schema.project.c.id == project_id)
            )
            .mappings()
            .first()
        )
        if existing is None:
            raise ValueError(f"project not found: {project_id}")
        prev_raw = existing["llm_overrides"]
        conn.execute(
            update(schema.project)
            .where(schema.project.c.id == project_id)
            .values(llm_overrides=payload)
        )
        append_event(
            conn,
            project_id=project_id,
            kind="project.llm_overrides_changed",
            payload={
                "prev_set": bool(prev_raw),
                "new_set": payload is not None,
            },
        )
        refreshed = (
            conn.execute(
                select(schema.project).where(schema.project.c.id == project_id)
            )
            .mappings()
            .first()
        )
    assert refreshed is not None
    return ProjectRow(**dict(refreshed))


def update_project_name(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    name: str,
) -> ProjectRow:
    """Rename a project, recording the change as an audit event.

    The Settings screen exposes this as an editable field so curators
    can fix typos without re-creating the project. Empty / whitespace-
    only names are rejected — a blank name would render as a void row
    on the recents list.
    """

    if not name or not name.strip():
        raise ValueError("name must be non-empty")
    new_name = name.strip()

    with _begin(engine_or_conn) as conn:
        existing = (
            conn.execute(
                select(schema.project).where(schema.project.c.id == project_id)
            )
            .mappings()
            .first()
        )
        if existing is None:
            raise ValueError(f"project not found: {project_id}")
        prev = str(existing["name"])
        if prev == new_name:
            return ProjectRow(**dict(existing))
        conn.execute(
            update(schema.project)
            .where(schema.project.c.id == project_id)
            .values(name=new_name)
        )
        append_event(
            conn,
            project_id=project_id,
            kind="project.renamed",
            payload={"prev_name": prev, "new_name": new_name},
        )
        refreshed = (
            conn.execute(
                select(schema.project).where(schema.project.c.id == project_id)
            )
            .mappings()
            .first()
        )
    assert refreshed is not None
    return ProjectRow(**dict(refreshed))


def update_project_budget(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    budget_usd: float | None,
) -> ProjectRow:
    """Set or clear the per-project USD budget cap (PRD F-LLM-8 / M4).

    Records a ``project.budget_changed`` event in the same transaction
    so the Inbox can surface the change in its alerts feed
    (``glossary-invariants.mdc``-style audit; M4 reuses the same
    pattern). Raises :class:`ValueError` when the project does not exist.
    """

    if budget_usd is not None and budget_usd < 0:
        raise ValueError("budget_usd must be non-negative or None")

    with _begin(engine_or_conn) as conn:
        existing = (
            conn.execute(
                select(schema.project).where(schema.project.c.id == project_id)
            )
            .mappings()
            .first()
        )
        if existing is None:
            raise ValueError(f"project not found: {project_id}")
        prev = existing["budget_usd"]
        conn.execute(
            update(schema.project)
            .where(schema.project.c.id == project_id)
            .values(budget_usd=budget_usd)
        )
        append_event(
            conn,
            project_id=project_id,
            kind="project.budget_changed",
            payload={
                "prev_budget_usd": float(prev) if prev is not None else None,
                "new_budget_usd": (
                    float(budget_usd) if budget_usd is not None else None
                ),
            },
        )
        refreshed = (
            conn.execute(
                select(schema.project).where(schema.project.c.id == project_id)
            )
            .mappings()
            .first()
        )
    assert refreshed is not None
    return ProjectRow(**dict(refreshed))


def append_event(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    ts: int | None = None,
) -> EventRow:
    """Append-only write to the ``event`` table (PRD §4.7 / F-P-2)."""

    row = EventRow(
        project_id=project_id,
        ts=ts or _now_unix(),
        kind=kind,
        payload=payload or {},
    )
    stmt = insert(schema.event).values(
        project_id=row.project_id,
        ts=row.ts,
        kind=row.kind,
        payload_json=json.dumps(row.payload, sort_keys=True),
    )
    with _begin(engine_or_conn) as conn:
        result = conn.execute(stmt)
    inserted_pk = result.inserted_primary_key
    return row.model_copy(
        update={"id": int(inserted_pk[0]) if inserted_pk is not None else None}
    )


def bulk_insert_chapters(
    engine_or_conn: Engine | Connection, rows: list[ChapterRow]
) -> None:
    """Insert chapter rows in a single transaction (resumability rule)."""

    if not rows:
        return
    payload = [r.model_dump() for r in rows]
    stmt = insert(schema.chapter)
    with _begin(engine_or_conn) as conn:
        conn.execute(stmt, payload)


def list_chapters(
    engine_or_conn: Engine | Connection, project_id: str
) -> list[ChapterRow]:
    stmt = (
        select(schema.chapter)
        .where(schema.chapter.c.project_id == project_id)
        .order_by(schema.chapter.c.spine_idx.asc())
    )
    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    return [ChapterRow(**dict(r)) for r in rows]


def bulk_insert_segments(
    engine_or_conn: Engine | Connection, rows: list[SegmentRow]
) -> None:
    if not rows:
        return
    payload = [
        {
            "id": r.id,
            "chapter_id": r.chapter_id,
            "idx": r.idx,
            "source_text": r.source_text,
            "source_hash": r.source_hash,
            "target_text": r.target_text,
            "status": r.status,
            "inline_skeleton": _encode_inline_skeleton(r),
        }
        for r in rows
    ]
    stmt = insert(schema.segment)
    with _begin(engine_or_conn) as conn:
        conn.execute(stmt, payload)


def list_segments(
    engine_or_conn: Engine | Connection, chapter_id: str
) -> list[SegmentRow]:
    stmt = (
        select(schema.segment)
        .where(schema.segment.c.chapter_id == chapter_id)
        .order_by(schema.segment.c.idx.asc())
    )
    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    out: list[SegmentRow] = []
    for r in rows:
        skel, host_path, host_part, host_total = _decode_inline_skeleton(
            r["inline_skeleton"]
        )
        out.append(
            SegmentRow(
                id=str(r["id"]),
                chapter_id=str(r["chapter_id"]),
                idx=int(r["idx"]),
                source_text=str(r["source_text"]),
                source_hash=str(r["source_hash"]),
                target_text=(
                    str(r["target_text"]) if r["target_text"] is not None else None
                ),
                status=str(r["status"]),
                inline_skeleton=skel,
                host_path=host_path,
                host_part=host_part,
                host_total_parts=host_total,
            )
        )
    return out


def segment_row_from(seg: Segment) -> SegmentRow:
    """Lift a runtime :class:`Segment` (from the format adapter) into a row."""

    return SegmentRow(
        id=seg.id,
        chapter_id=seg.chapter_id,
        idx=seg.idx,
        source_text=seg.source_text,
        source_hash=seg.source_hash,
        target_text=seg.target_text,
        status=schema.SegmentStatus.PENDING,
        inline_skeleton=list(seg.inline_skeleton),
        host_path=seg.host_path,
        host_part=seg.host_part,
        host_total_parts=seg.host_total_parts,
    )


def segment_row_to(row: SegmentRow) -> Segment:
    """Inverse of :func:`segment_row_from`."""

    return Segment(
        id=row.id,
        chapter_id=row.chapter_id,
        idx=row.idx,
        source_text=row.source_text,
        source_hash=row.source_hash,
        target_text=row.target_text,
        inline_skeleton=list(row.inline_skeleton),
        host_path=row.host_path,
        host_part=row.host_part,
        host_total_parts=row.host_total_parts,
    )


def update_segment_translation(
    engine_or_conn: Engine | Connection,
    *,
    segment_id: str,
    target_text: str | None,
    status: str,
) -> None:
    """Update ``segment.target_text`` + ``segment.status`` in one statement."""

    stmt = (
        update(schema.segment)
        .where(schema.segment.c.id == segment_id)
        .values(target_text=target_text, status=status)
    )
    with _begin(engine_or_conn) as conn:
        conn.execute(stmt)


def update_segment_target_text(
    engine_or_conn: Engine | Connection,
    *,
    segment_id: str,
    target_text: str,
) -> None:
    """Rewrite ``segment.target_text`` without touching ``status``.

    Used by retroactive cleanup flows (``epublate
    sanitize-typography``) that need to repair stored target text
    without bumping the segment's ``flagged`` / ``translated``
    status — those flags reflect human-curated decisions and a
    pure-text rewrite must not silently re-mark them.
    """

    stmt = (
        update(schema.segment)
        .where(schema.segment.c.id == segment_id)
        .values(target_text=target_text)
    )
    with _begin(engine_or_conn) as conn:
        conn.execute(stmt)


def rewrite_segment_source(
    engine_or_conn: Engine | Connection,
    *,
    segment_id: str,
    source_text: str,
    source_hash: str,
    target_text: str | None,
    skeleton: list[InlineToken],
    host_path: str,
    host_part: int,
    host_total_parts: int,
) -> None:
    """Rewrite a segment's source / target / skeleton in one statement.

    Used by the in-place ``Project.expand_typographic_entities``
    migration: when typographic entity placeholders (``[[T0]]`` for
    ``&rsquo;`` etc.) are expanded inline to literal Unicode chars,
    the row's ``source_text`` (and therefore ``source_hash``) and
    ``inline_skeleton`` change together. ``target_text`` is rewritten
    in lockstep so any translation that preserved the placeholder
    reads cleanly afterwards. ``status`` is intentionally untouched
    — the migration is a pure-text rewrite and curator decisions
    (``flagged`` / ``approved``) must survive it.
    """

    stub = SegmentRow(
        id=segment_id,
        chapter_id="",
        idx=0,
        source_text="",
        source_hash="",
        inline_skeleton=skeleton,
        host_path=host_path,
        host_part=host_part,
        host_total_parts=host_total_parts,
    )
    blob = _encode_inline_skeleton(stub)
    stmt = (
        update(schema.segment)
        .where(schema.segment.c.id == segment_id)
        .values(
            source_text=source_text,
            source_hash=source_hash,
            target_text=target_text,
            inline_skeleton=blob,
        )
    )
    with _begin(engine_or_conn) as conn:
        conn.execute(stmt)


def update_segment_skeleton(
    engine_or_conn: Engine | Connection,
    *,
    segment_id: str,
    skeleton: list[InlineToken],
    host_path: str,
    host_part: int,
    host_total_parts: int,
) -> None:
    """Rewrite ``segment.inline_skeleton`` for an existing row.

    Used by the ``repair`` flow when a segmenter change shifts host
    XPaths in the original ePub (e.g. the orphan-hoist pass adds new
    sibling wrappers): we keep the row's translation but update the
    skeleton blob so the export-side XPath lookup resolves to the
    correct DOM node again.
    """

    stub = SegmentRow(
        id=segment_id,
        chapter_id="",
        idx=0,
        source_text="",
        source_hash="",
        inline_skeleton=skeleton,
        host_path=host_path,
        host_part=host_part,
        host_total_parts=host_total_parts,
    )
    blob = _encode_inline_skeleton(stub)
    stmt = (
        update(schema.segment)
        .where(schema.segment.c.id == segment_id)
        .values(inline_skeleton=blob)
    )
    with _begin(engine_or_conn) as conn:
        conn.execute(stmt)


def update_segment_status(
    engine_or_conn: Engine | Connection,
    *,
    segment_id: str,
    status: str,
) -> None:
    """Flip ``segment.status`` without touching ``target_text``.

    Used by the cascade flow (M3): when a confirmed/locked entry's target
    term changes, affected segments revert to ``pending`` while their old
    translation is preserved in the ``event`` log for history.
    """

    stmt = (
        update(schema.segment)
        .where(schema.segment.c.id == segment_id)
        .values(status=status)
    )
    with _begin(engine_or_conn) as conn:
        conn.execute(stmt)


def get_segment(
    engine_or_conn: Engine | Connection, segment_id: str
) -> SegmentRow | None:
    stmt = select(schema.segment).where(schema.segment.c.id == segment_id)
    with _begin(engine_or_conn) as conn:
        row = conn.execute(stmt).mappings().first()
    if row is None:
        return None
    skel, host_path, host_part, host_total = _decode_inline_skeleton(
        row["inline_skeleton"]
    )
    return SegmentRow(
        id=str(row["id"]),
        chapter_id=str(row["chapter_id"]),
        idx=int(row["idx"]),
        source_text=str(row["source_text"]),
        source_hash=str(row["source_hash"]),
        target_text=(
            str(row["target_text"]) if row["target_text"] is not None else None
        ),
        status=str(row["status"]),
        inline_skeleton=skel,
        host_path=host_path,
        host_part=host_part,
        host_total_parts=host_total,
    )


def insert_llm_call(
    engine_or_conn: Engine | Connection,
    row: LLMCallRow,
) -> LLMCallRow:
    """Append one ``llm_call`` row (PRD §6.4 / F-LLM-7).

    Returns the row exactly as inserted (with ``created_at`` filled in if
    the caller passed ``0``).
    """

    final = row
    if row.created_at == 0:
        final = row.model_copy(update={"created_at": _now_unix()})
    stmt = insert(schema.llm_call).values(
        id=final.id,
        project_id=final.project_id,
        segment_id=final.segment_id,
        purpose=final.purpose,
        model=final.model,
        prompt_tokens=final.prompt_tokens,
        completion_tokens=final.completion_tokens,
        cost_usd=final.cost_usd,
        cache_hit=1 if final.cache_hit else 0,
        cache_key=final.cache_key,
        request_json=final.request_json,
        response_json=final.response_json,
        created_at=final.created_at,
    )
    with _begin(engine_or_conn) as conn:
        conn.execute(stmt)
    return final


def find_llm_call_by_cache_key(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    cache_key: str,
) -> LLMCallRow | None:
    """Most-recent ``llm_call`` row for ``(project_id, cache_key)`` or ``None``.

    Cache hits are oldest-first irrelevant; the most recent successful row
    is the canonical one because re-translation cascades (M3) update the
    cache by appending newer rows.
    """

    stmt = (
        select(schema.llm_call)
        .where(schema.llm_call.c.project_id == project_id)
        .where(schema.llm_call.c.cache_key == cache_key)
        .order_by(schema.llm_call.c.created_at.desc())
        .limit(1)
    )
    with _begin(engine_or_conn) as conn:
        row = conn.execute(stmt).mappings().first()
    if row is None:
        return None
    return LLMCallRow(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        segment_id=(str(row["segment_id"]) if row["segment_id"] is not None else None),
        purpose=str(row["purpose"]),
        model=str(row["model"]),
        prompt_tokens=(
            int(row["prompt_tokens"]) if row["prompt_tokens"] is not None else None
        ),
        completion_tokens=(
            int(row["completion_tokens"])
            if row["completion_tokens"] is not None
            else None
        ),
        cost_usd=(float(row["cost_usd"]) if row["cost_usd"] is not None else None),
        cache_hit=bool(int(row["cache_hit"] or 0)),
        cache_key=(str(row["cache_key"]) if row["cache_key"] is not None else None),
        request_json=(
            str(row["request_json"]) if row["request_json"] is not None else None
        ),
        response_json=(
            str(row["response_json"]) if row["response_json"] is not None else None
        ),
        created_at=int(row["created_at"]),
    )


def list_llm_calls(
    engine_or_conn: Engine | Connection,
    project_id: str,
    *,
    limit: int | None = None,
    descending: bool = False,
) -> list[LLMCallRow]:
    """List ``llm_call`` rows for the project (defaults to ascending, full list).

    The Dashboard's LLM activity panel uses ``limit + descending=True``
    to get the most recent N calls cheaply; everywhere else (cost
    rollups, snapshot reconciliation) keeps the original ascending /
    unbounded contract.
    """

    order_col = (
        schema.llm_call.c.created_at.desc()
        if descending
        else schema.llm_call.c.created_at.asc()
    )
    stmt = (
        select(schema.llm_call)
        .where(schema.llm_call.c.project_id == project_id)
        .order_by(order_col)
    )
    if limit is not None and limit > 0:
        stmt = stmt.limit(limit)
    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        LLMCallRow(
            id=str(r["id"]),
            project_id=str(r["project_id"]),
            segment_id=(str(r["segment_id"]) if r["segment_id"] is not None else None),
            purpose=str(r["purpose"]),
            model=str(r["model"]),
            prompt_tokens=(
                int(r["prompt_tokens"]) if r["prompt_tokens"] is not None else None
            ),
            completion_tokens=(
                int(r["completion_tokens"])
                if r["completion_tokens"] is not None
                else None
            ),
            cost_usd=(float(r["cost_usd"]) if r["cost_usd"] is not None else None),
            cache_hit=bool(int(r["cache_hit"] or 0)),
            cache_key=(str(r["cache_key"]) if r["cache_key"] is not None else None),
            request_json=(
                str(r["request_json"]) if r["request_json"] is not None else None
            ),
            response_json=(
                str(r["response_json"]) if r["response_json"] is not None else None
            ),
            created_at=int(r["created_at"]),
        )
        for r in rows
    ]


def list_events(engine_or_conn: Engine | Connection, project_id: str) -> list[EventRow]:
    stmt = (
        select(schema.event)
        .where(schema.event.c.project_id == project_id)
        .order_by(schema.event.c.id.asc())
    )
    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        EventRow(
            id=int(r["id"]),
            project_id=str(r["project_id"]),
            ts=int(r["ts"]),
            kind=str(r["kind"]),
            payload=json.loads(r["payload_json"]) if r["payload_json"] else {},
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Glossary (lore bible) — PRD §4.3 / M3
# ---------------------------------------------------------------------------


def _glossary_entry_from_row(row: dict[str, Any]) -> GlossaryEntry:
    raw_source_term = row.get("source_term")
    source_term = str(raw_source_term) if raw_source_term is not None else None
    raw_known = row.get("source_known")
    # Legacy DBs that haven't run migration 0005 yet have no
    # ``source_known`` column; treat the row as source-known so the
    # validator keeps its pre-Lore-Book hard-fail behaviour.
    source_known = True if raw_known is None else bool(int(raw_known))
    return GlossaryEntry(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        type=str(row["type"]),  # type: ignore[arg-type]
        source_term=source_term,
        target_term=str(row["target_term"]),
        gender=(str(row["gender"]) if row["gender"] is not None else None),  # type: ignore[arg-type]
        status=str(row["status"]),  # type: ignore[arg-type]
        notes=(str(row["notes"]) if row["notes"] is not None else None),
        first_seen_segment_id=(
            str(row["first_seen_segment_id"])
            if row["first_seen_segment_id"] is not None
            else None
        ),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        source_known=source_known,
    )


def create_glossary_entry(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    source_term: str | None,
    target_term: str,
    type: EntityType = "term",
    status: GlossaryStatusLiteral = "proposed",
    gender: GenderTag | None = None,
    notes: str | None = None,
    first_seen_segment_id: str | None = None,
    source_aliases: Iterable[str] = (),
    target_aliases: Iterable[str] = (),
    entry_id: str | None = None,
    created_at: int | None = None,
    updated_at: int | None = None,
    source_known: bool | None = None,
) -> GlossaryEntry:
    """Insert one glossary entry plus its aliases in a single transaction.

    The aliases are de-duplicated against the canonical term (a source
    alias equal to ``source_term`` is dropped — the matcher already checks
    the canonical term separately) and within their own side.

    When ``source_term`` is ``None`` (target-only Lore Book entry), the
    caller must pass either ``source_known=False`` explicitly or rely on
    the auto-derived default (``False`` when no source spelling is
    provided). Project-DB callers should always pass a string
    ``source_term`` — the existing pipeline never produces ``None``.
    """

    if source_known is None:
        source_known = source_term is not None
    if source_term is None and source_known:
        raise ValueError(
            "source_known=True requires a non-empty source_term; "
            "for target-only Lore Book entries pass source_known=False"
        )

    now = _now_unix()
    entry = GlossaryEntry(
        id=entry_id or _new_id(),
        project_id=project_id,
        type=type,
        source_term=source_term,
        target_term=target_term,
        gender=gender,
        status=status,
        notes=notes,
        first_seen_segment_id=first_seen_segment_id,
        created_at=created_at or now,
        updated_at=updated_at or created_at or now,
        source_known=source_known,
    )
    payload = entry.model_dump()
    payload["source_known"] = 1 if entry.source_known else 0
    insert_entry = insert(schema.glossary_entry).values(**payload)

    src_set: list[str] = []
    seen_src: set[str] = set()
    if source_term:
        seen_src.add(source_term)
    for alias in source_aliases:
        if alias and alias not in seen_src:
            seen_src.add(alias)
            src_set.append(alias)
    tgt_set: list[str] = []
    seen_tgt: set[str] = {target_term}
    for alias in target_aliases:
        if alias and alias not in seen_tgt:
            seen_tgt.add(alias)
            tgt_set.append(alias)
    alias_payload = [
        {"id": _new_id(), "entry_id": entry.id, "side": "source", "text": text}
        for text in src_set
    ] + [
        {"id": _new_id(), "entry_id": entry.id, "side": "target", "text": text}
        for text in tgt_set
    ]

    with _begin(engine_or_conn) as conn:
        conn.execute(insert_entry)
        if alias_payload:
            conn.execute(insert(schema.glossary_alias), alias_payload)
    return entry


def get_glossary_entry(
    engine_or_conn: Engine | Connection, entry_id: str
) -> GlossaryEntryWithAliases | None:
    stmt = select(schema.glossary_entry).where(schema.glossary_entry.c.id == entry_id)
    with _begin(engine_or_conn) as conn:
        row = conn.execute(stmt).mappings().first()
        if row is None:
            return None
        entry = _glossary_entry_from_row(dict(row))
        aliases = (
            conn.execute(
                select(schema.glossary_alias).where(
                    schema.glossary_alias.c.entry_id == entry_id
                )
            )
            .mappings()
            .all()
        )
    src = [str(r["text"]) for r in aliases if r["side"] == "source"]
    tgt = [str(r["text"]) for r in aliases if r["side"] == "target"]
    return GlossaryEntryWithAliases(
        entry=entry,
        source_aliases=sorted(src),
        target_aliases=sorted(tgt),
    )


def list_glossary_entries(
    engine_or_conn: Engine | Connection,
    project_id: str,
    *,
    status: GlossaryStatusLiteral | None = None,
) -> list[GlossaryEntryWithAliases]:
    """Return every glossary entry for ``project_id`` with aliases attached.

    Sorted by ``source_term`` for stable hashing in
    :func:`epublate.glossary.enforcer.glossary_hash`.
    """

    entry_stmt = select(schema.glossary_entry).where(
        schema.glossary_entry.c.project_id == project_id
    )
    if status is not None:
        entry_stmt = entry_stmt.where(schema.glossary_entry.c.status == status)
    entry_stmt = entry_stmt.order_by(
        schema.glossary_entry.c.source_term.asc(),
        schema.glossary_entry.c.id.asc(),
    )

    with _begin(engine_or_conn) as conn:
        entry_rows = conn.execute(entry_stmt).mappings().all()
        if not entry_rows:
            return []
        ids = [str(r["id"]) for r in entry_rows]
        alias_rows = (
            conn.execute(
                select(schema.glossary_alias).where(
                    schema.glossary_alias.c.entry_id.in_(ids)
                )
            )
            .mappings()
            .all()
        )

    aliases_by_entry: dict[str, tuple[list[str], list[str]]] = {
        eid: ([], []) for eid in ids
    }
    for r in alias_rows:
        bucket = aliases_by_entry[str(r["entry_id"])]
        if r["side"] == "source":
            bucket[0].append(str(r["text"]))
        else:
            bucket[1].append(str(r["text"]))

    out: list[GlossaryEntryWithAliases] = []
    for r in entry_rows:
        entry = _glossary_entry_from_row(dict(r))
        src, tgt = aliases_by_entry[entry.id]
        out.append(
            GlossaryEntryWithAliases(
                entry=entry,
                source_aliases=sorted(src),
                target_aliases=sorted(tgt),
            )
        )
    return out


def find_glossary_entry_by_source_term(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    source_term: str,
    type: EntityType | None = None,
) -> GlossaryEntry | None:
    """Lookup helper used by the auto-proposer to dedupe candidates."""

    stmt = (
        select(schema.glossary_entry)
        .where(schema.glossary_entry.c.project_id == project_id)
        .where(schema.glossary_entry.c.source_term == source_term)
    )
    if type is not None:
        stmt = stmt.where(schema.glossary_entry.c.type == type)
    stmt = stmt.limit(1)
    with _begin(engine_or_conn) as conn:
        row = conn.execute(stmt).mappings().first()
    if row is None:
        return None
    return _glossary_entry_from_row(dict(row))


def update_glossary_entry(
    engine_or_conn: Engine | Connection,
    *,
    entry_id: str,
    target_term: str | None = None,
    status: GlossaryStatusLiteral | None = None,
    type: EntityType | None = None,
    gender: GenderTag | None = None,
    notes: str | None = None,
    reason: str | None = None,
) -> GlossaryEntry:
    """Update fields on an existing entry and record a revision when needed.

    A revision row is appended whenever ``target_term`` or ``status``
    actually change (PRD F-LB-6; ``glossary-invariants.mdc`` §3). The
    write commits in a single transaction so revisions can never lag the
    entry's current state.
    """

    with _begin(engine_or_conn) as conn:
        existing_row = (
            conn.execute(
                select(schema.glossary_entry).where(
                    schema.glossary_entry.c.id == entry_id
                )
            )
            .mappings()
            .first()
        )
        if existing_row is None:
            raise ValueError(f"glossary entry not found: {entry_id}")
        existing = _glossary_entry_from_row(dict(existing_row))

        new_values: dict[str, Any] = {"updated_at": _now_unix()}
        target_changed = target_term is not None and target_term != existing.target_term
        status_changed = status is not None and status != existing.status
        if target_changed:
            new_values["target_term"] = target_term
        if status_changed:
            new_values["status"] = status
        if type is not None and type != existing.type:
            new_values["type"] = type
        if gender is not None and gender != existing.gender:
            new_values["gender"] = gender
        if notes is not None and notes != existing.notes:
            new_values["notes"] = notes

        if len(new_values) == 1:
            return existing

        conn.execute(
            update(schema.glossary_entry)
            .where(schema.glossary_entry.c.id == entry_id)
            .values(**new_values)
        )

        if target_changed or status_changed:
            conn.execute(
                insert(schema.glossary_revision).values(
                    id=_new_id(),
                    entry_id=entry_id,
                    prev_target_term=existing.target_term,
                    new_target_term=(
                        target_term if target_changed else existing.target_term
                    ),
                    reason=reason
                    or (
                        f"status: {existing.status} -> {status}"
                        if status_changed and not target_changed
                        else None
                    ),
                    created_at=_now_unix(),
                )
            )

    refreshed = get_glossary_entry(engine_or_conn, entry_id)
    assert refreshed is not None  # we just updated it
    return refreshed.entry


def delete_glossary_entry(engine_or_conn: Engine | Connection, entry_id: str) -> None:
    """Remove an entry; cascade FKs handle aliases/revisions/mentions."""

    with _begin(engine_or_conn) as conn:
        conn.execute(
            delete(schema.glossary_entry).where(schema.glossary_entry.c.id == entry_id)
        )


def find_duplicate_source_terms(
    engine_or_conn: Engine | Connection,
    project_id: str,
) -> list[list[GlossaryEntryWithAliases]]:
    """Return every group of entries that share a non-empty source term.

    The auto-proposer used to dedup by ``(source_term, type)`` which let
    the same proper noun appear with multiple types (the curator's
    "House → Câmara" / "House → Casa" issue). We've since switched
    auto-propose to dedupe on source term alone; this helper surfaces
    the historical duplicates so the curator can merge them via the
    Glossary screen.

    Returns one list per duplicate group. Each group is sorted with the
    "most useful winner" first (locked > confirmed > proposed; specific
    type before generic ``term``; older row before newer) so a
    no-decision curator can accept the head as the keeper. Target-only
    entries (``source_term IS NULL``) are excluded — they have no
    source key to dedupe on.
    """

    entries = list_glossary_entries(engine_or_conn, project_id)
    by_source: dict[str, list[GlossaryEntryWithAliases]] = {}
    for ent in entries:
        if not ent.source_term:
            continue
        by_source.setdefault(ent.source_term, []).append(ent)

    status_rank = {"locked": 0, "confirmed": 1, "proposed": 2}

    def _rank(e: GlossaryEntryWithAliases) -> tuple[int, int, int]:
        return (
            status_rank.get(e.status, 99),
            0 if e.entry.type != "term" else 1,
            e.entry.created_at,
        )

    return [sorted(group, key=_rank) for group in by_source.values() if len(group) > 1]


def merge_glossary_entries(
    engine_or_conn: Engine | Connection,
    *,
    winner_id: str,
    loser_ids: Sequence[str],
    reason: str | None = None,
) -> int:
    """Fold ``loser_ids`` into ``winner_id`` and delete the losers.

    Mechanics:

    * Each loser's canonical source term that differs from the
      winner's is added as a source-side alias on the winner. Each
      loser's existing source aliases come along too. Same for target
      aliases (against the winner's canonical target term).
    * ``entity_mention`` rows pointing at the loser are re-pointed to
      the winner so the lore bible's audit trail is preserved.
    * Loser rows are deleted; cascading FKs sweep the remaining
      ``glossary_alias`` / ``glossary_revision`` rows.
    * A ``glossary_revision`` row is appended to the winner with the
      provided ``reason`` (defaults to ``merge``). Returns the count of
      losers actually removed.

    Runs in a single transaction so a crash never leaves a half-merged
    glossary (db-and-persistence rule §2 / glossary-invariants §5).
    """

    if not loser_ids:
        return 0

    with _begin(engine_or_conn) as conn:
        winner_row = (
            conn.execute(
                select(schema.glossary_entry).where(
                    schema.glossary_entry.c.id == winner_id
                )
            )
            .mappings()
            .first()
        )
        if winner_row is None:
            raise ValueError(f"glossary entry not found: {winner_id}")
        winner_source = (
            str(winner_row["source_term"])
            if winner_row["source_term"] is not None
            else None
        )
        winner_target = str(winner_row["target_term"])

        existing_alias_rows = (
            conn.execute(
                select(schema.glossary_alias).where(
                    schema.glossary_alias.c.entry_id == winner_id
                )
            )
            .mappings()
            .all()
        )
        existing_src: set[str] = {
            str(r["text"]) for r in existing_alias_rows if r["side"] == "source"
        }
        existing_tgt: set[str] = {
            str(r["text"]) for r in existing_alias_rows if r["side"] == "target"
        }

        added = 0
        for lid in loser_ids:
            if lid == winner_id:
                continue
            loser_row = (
                conn.execute(
                    select(schema.glossary_entry).where(
                        schema.glossary_entry.c.id == lid
                    )
                )
                .mappings()
                .first()
            )
            if loser_row is None:
                continue
            new_aliases: list[dict[str, Any]] = []
            loser_source = (
                str(loser_row["source_term"])
                if loser_row["source_term"] is not None
                else None
            )
            loser_target = str(loser_row["target_term"])
            if (
                loser_source
                and loser_source != winner_source
                and loser_source not in existing_src
            ):
                existing_src.add(loser_source)
                new_aliases.append(
                    {
                        "id": _new_id(),
                        "entry_id": winner_id,
                        "side": "source",
                        "text": loser_source,
                    }
                )
            if (
                loser_target
                and loser_target != winner_target
                and loser_target not in existing_tgt
            ):
                existing_tgt.add(loser_target)
                new_aliases.append(
                    {
                        "id": _new_id(),
                        "entry_id": winner_id,
                        "side": "target",
                        "text": loser_target,
                    }
                )
            for alias_row in conn.execute(
                select(schema.glossary_alias).where(
                    schema.glossary_alias.c.entry_id == lid
                )
            ).mappings():
                side = str(alias_row["side"])
                text = str(alias_row["text"])
                if side == "source":
                    if text in existing_src or text == winner_source:
                        continue
                    existing_src.add(text)
                else:
                    if text in existing_tgt or text == winner_target:
                        continue
                    existing_tgt.add(text)
                new_aliases.append(
                    {
                        "id": _new_id(),
                        "entry_id": winner_id,
                        "side": side,
                        "text": text,
                    }
                )
            if new_aliases:
                conn.execute(insert(schema.glossary_alias), new_aliases)
            conn.execute(
                update(schema.entity_mention)
                .where(schema.entity_mention.c.entry_id == lid)
                .values(entry_id=winner_id)
            )
            conn.execute(
                delete(schema.glossary_entry).where(schema.glossary_entry.c.id == lid)
            )
            added += 1

        if added:
            conn.execute(
                insert(schema.glossary_revision).values(
                    id=_new_id(),
                    entry_id=winner_id,
                    prev_target_term=winner_target,
                    new_target_term=winner_target,
                    reason=reason or "merge",
                    created_at=_now_unix(),
                )
            )
            conn.execute(
                update(schema.glossary_entry)
                .where(schema.glossary_entry.c.id == winner_id)
                .values(updated_at=_now_unix())
            )
        return added


def set_aliases(
    engine_or_conn: Engine | Connection,
    *,
    entry_id: str,
    source_aliases: Iterable[str] = (),
    target_aliases: Iterable[str] = (),
) -> None:
    """Replace all aliases for ``entry_id`` with the given sets.

    De-duplicates within each side. The canonical term is *not* added
    here — the matcher already includes it explicitly.
    """

    src_clean: list[str] = []
    seen_src: set[str] = set()
    for alias in source_aliases:
        if alias and alias not in seen_src:
            seen_src.add(alias)
            src_clean.append(alias)
    tgt_clean: list[str] = []
    seen_tgt: set[str] = set()
    for alias in target_aliases:
        if alias and alias not in seen_tgt:
            seen_tgt.add(alias)
            tgt_clean.append(alias)

    payload = [
        {"id": _new_id(), "entry_id": entry_id, "side": "source", "text": text}
        for text in src_clean
    ] + [
        {"id": _new_id(), "entry_id": entry_id, "side": "target", "text": text}
        for text in tgt_clean
    ]

    with _begin(engine_or_conn) as conn:
        conn.execute(
            delete(schema.glossary_alias).where(
                schema.glossary_alias.c.entry_id == entry_id
            )
        )
        if payload:
            conn.execute(insert(schema.glossary_alias), payload)


def list_aliases(
    engine_or_conn: Engine | Connection, entry_id: str
) -> list[GlossaryAlias]:
    stmt = (
        select(schema.glossary_alias)
        .where(schema.glossary_alias.c.entry_id == entry_id)
        .order_by(
            schema.glossary_alias.c.side.asc(),
            schema.glossary_alias.c.text.asc(),
        )
    )
    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        GlossaryAlias(
            id=str(r["id"]),
            entry_id=str(r["entry_id"]),
            side=str(r["side"]),  # type: ignore[arg-type]
            text=str(r["text"]),
        )
        for r in rows
    ]


def list_glossary_revisions(
    engine_or_conn: Engine | Connection, entry_id: str
) -> list[GlossaryRevision]:
    stmt = (
        select(schema.glossary_revision)
        .where(schema.glossary_revision.c.entry_id == entry_id)
        .order_by(schema.glossary_revision.c.created_at.asc())
    )
    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        GlossaryRevision(
            id=str(r["id"]),
            entry_id=str(r["entry_id"]),
            prev_target_term=(
                str(r["prev_target_term"])
                if r["prev_target_term"] is not None
                else None
            ),
            new_target_term=(
                str(r["new_target_term"]) if r["new_target_term"] is not None else None
            ),
            reason=(str(r["reason"]) if r["reason"] is not None else None),
            created_at=int(r["created_at"]),
        )
        for r in rows
    ]


def record_mentions(
    engine_or_conn: Engine | Connection,
    *,
    segment_id: str,
    mentions: Iterable[tuple[str, int | None, int | None]],
) -> None:
    """Replace ``entity_mention`` rows for ``segment_id``.

    ``mentions`` is an iterable of ``(entry_id, span_start, span_end)``;
    we de-duplicate by ``(entry_id, span_start, span_end)`` so a noisy
    matcher doesn't bloat the table.
    """

    seen: set[tuple[str, int | None, int | None]] = set()
    payload: list[dict[str, Any]] = []
    for entry_id, start, end in mentions:
        key = (entry_id, start, end)
        if key in seen:
            continue
        seen.add(key)
        payload.append(
            {
                "id": _new_id(),
                "segment_id": segment_id,
                "entry_id": entry_id,
                "source_span_start": start,
                "source_span_end": end,
            }
        )

    with _begin(engine_or_conn) as conn:
        conn.execute(
            delete(schema.entity_mention).where(
                schema.entity_mention.c.segment_id == segment_id
            )
        )
        if payload:
            conn.execute(insert(schema.entity_mention), payload)


def list_mentions(
    engine_or_conn: Engine | Connection,
    *,
    segment_id: str | None = None,
    entry_id: str | None = None,
) -> list[EntityMention]:
    stmt = select(schema.entity_mention)
    if segment_id is not None:
        stmt = stmt.where(schema.entity_mention.c.segment_id == segment_id)
    if entry_id is not None:
        stmt = stmt.where(schema.entity_mention.c.entry_id == entry_id)
    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        EntityMention(
            id=str(r["id"]),
            segment_id=str(r["segment_id"]),
            entry_id=str(r["entry_id"]),
            source_span_start=(
                int(r["source_span_start"])
                if r["source_span_start"] is not None
                else None
            ),
            source_span_end=(
                int(r["source_span_end"]) if r["source_span_end"] is not None else None
            ),
        )
        for r in rows
    ]


class MentionCounts(BaseModel):
    """Aggregate counts a glossary entry's mention rows expand to.

    ``mentions`` is the total :class:`EntityMention` row count for the
    entry — multiple matches in the same segment count separately so
    the curator can see how often a term actually fires. ``segments``
    is the count of distinct segments those rows reach, which is the
    more useful "spread" number when a single segment hits a term ten
    times. The Glossary screen surfaces ``mentions`` in the table column
    and both numbers in the detail pane.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mentions: int = 0
    segments: int = 0


def count_mentions_per_entry(
    engine_or_conn: Engine | Connection, project_id: str
) -> dict[str, MentionCounts]:
    """Per-entry mention totals for a whole project, in one query.

    Returns ``{entry_id: MentionCounts(mentions, segments)}``. We aggregate
    in SQL (``COUNT(*)`` for total, ``COUNT(DISTINCT segment_id)`` for
    spread) and join through ``segment → chapter`` to scope by project so
    a Lore Book editor never sees mentions from a sibling translation
    project. Entries with zero mentions are omitted; callers should
    treat a missing key as ``MentionCounts()``.
    """

    stmt = (
        select(
            schema.entity_mention.c.entry_id,
            func.count().label("mentions"),
            func.count(func.distinct(schema.entity_mention.c.segment_id)).label(
                "segments"
            ),
        )
        .join(
            schema.segment,
            schema.segment.c.id == schema.entity_mention.c.segment_id,
        )
        .join(
            schema.chapter,
            schema.chapter.c.id == schema.segment.c.chapter_id,
        )
        .where(schema.chapter.c.project_id == project_id)
        .group_by(schema.entity_mention.c.entry_id)
    )
    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    return {
        str(r["entry_id"]): MentionCounts(
            mentions=int(r["mentions"]),
            segments=int(r["segments"]),
        )
        for r in rows
    }


class OccurrenceRow(BaseModel):
    """One observed use of a glossary entry, with enough context to navigate.

    Joins :class:`EntityMention` with ``segment`` and ``chapter`` so the
    Glossary "Show occurrences" modal can render a navigable list
    (chapter title + spine_idx, segment idx, source/target snippets,
    matched span) without a per-row N+1. Sorted by ``spine_idx`` →
    ``segment.idx`` → ``source_span_start`` so the curator sees uses
    in book order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    mention_id: str
    segment_id: str
    segment_idx: int
    chapter_id: str
    chapter_spine_idx: int
    chapter_title: str | None
    source_text: str
    target_text: str | None
    source_span_start: int | None
    source_span_end: int | None


def list_occurrences(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    entry_id: str,
) -> list[OccurrenceRow]:
    """All :class:`EntityMention` rows for ``entry_id`` with segment context.

    A Lore Book project has no chapters/segments so this always returns
    ``[]`` for those entries — that's fine; the modal renders an empty
    state. Project-scoped via the ``chapter.project_id`` join so a
    cross-project mention (theoretically possible if a curator ran
    ``record_mentions`` against the wrong project) cannot leak into
    another project's view.
    """

    stmt = (
        select(
            schema.entity_mention.c.id.label("mention_id"),
            schema.entity_mention.c.source_span_start,
            schema.entity_mention.c.source_span_end,
            schema.segment.c.id.label("segment_id"),
            schema.segment.c.idx.label("segment_idx"),
            schema.segment.c.source_text,
            schema.segment.c.target_text,
            schema.chapter.c.id.label("chapter_id"),
            schema.chapter.c.spine_idx.label("chapter_spine_idx"),
            schema.chapter.c.title.label("chapter_title"),
        )
        .join(
            schema.segment,
            schema.segment.c.id == schema.entity_mention.c.segment_id,
        )
        .join(
            schema.chapter,
            schema.chapter.c.id == schema.segment.c.chapter_id,
        )
        .where(schema.entity_mention.c.entry_id == entry_id)
        .where(schema.chapter.c.project_id == project_id)
        .order_by(
            schema.chapter.c.spine_idx.asc(),
            schema.segment.c.idx.asc(),
            schema.entity_mention.c.source_span_start.asc().nulls_last(),
        )
    )
    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    return [
        OccurrenceRow(
            mention_id=str(r["mention_id"]),
            segment_id=str(r["segment_id"]),
            segment_idx=int(r["segment_idx"]),
            chapter_id=str(r["chapter_id"]),
            chapter_spine_idx=int(r["chapter_spine_idx"]),
            chapter_title=(
                str(r["chapter_title"]) if r["chapter_title"] is not None else None
            ),
            source_text=str(r["source_text"]),
            target_text=(
                str(r["target_text"]) if r["target_text"] is not None else None
            ),
            source_span_start=(
                int(r["source_span_start"])
                if r["source_span_start"] is not None
                else None
            ),
            source_span_end=(
                int(r["source_span_end"]) if r["source_span_end"] is not None else None
            ),
        )
        for r in rows
    ]


def list_segments_by_status(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    status: str,
    chapter_ids: tuple[str, ...] | None = None,
) -> list[SegmentRow]:
    """Project-wide segments filtered by ``status`` (and optionally chapters).

    Used by the M4 Inbox to list ``flagged`` segments and by the batch
    runner to enumerate ``pending`` work. Sorted by spine_idx then idx
    so the curator sees them in book order.
    """

    stmt = (
        select(schema.segment)
        .join(schema.chapter, schema.chapter.c.id == schema.segment.c.chapter_id)
        .where(schema.chapter.c.project_id == project_id)
        .where(schema.segment.c.status == status)
    )
    if chapter_ids is not None:
        if not chapter_ids:
            return []
        stmt = stmt.where(schema.segment.c.chapter_id.in_(chapter_ids))
    stmt = stmt.order_by(schema.chapter.c.spine_idx.asc(), schema.segment.c.idx.asc())

    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    return [_segment_row_from_mapping(r) for r in rows]


def _segment_row_from_mapping(r: Any) -> SegmentRow:
    """Decode one ``segment`` row mapping into a :class:`SegmentRow`."""

    skel, host_path, host_part, host_total = _decode_inline_skeleton(
        r["inline_skeleton"]
    )
    return SegmentRow(
        id=str(r["id"]),
        chapter_id=str(r["chapter_id"]),
        idx=int(r["idx"]),
        source_text=str(r["source_text"]),
        source_hash=str(r["source_hash"]),
        target_text=(str(r["target_text"]) if r["target_text"] is not None else None),
        status=str(r["status"]),
        inline_skeleton=skel,
        host_path=host_path,
        host_part=host_part,
        host_total_parts=host_total,
    )


def list_segments_for_project(
    engine_or_conn: Engine | Connection, project_id: str
) -> list[SegmentRow]:
    """Cascade helper: every segment in a project, regardless of chapter.

    Used by :mod:`epublate.glossary.cascade` to scan all source/target
    text for affected matches without N+1ing across chapters.
    """

    stmt = (
        select(schema.segment)
        .join(schema.chapter, schema.chapter.c.id == schema.segment.c.chapter_id)
        .where(schema.chapter.c.project_id == project_id)
        .order_by(schema.chapter.c.spine_idx.asc(), schema.segment.c.idx.asc())
    )
    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    out: list[SegmentRow] = []
    for r in rows:
        skel, host_path, host_part, host_total = _decode_inline_skeleton(
            r["inline_skeleton"]
        )
        out.append(
            SegmentRow(
                id=str(r["id"]),
                chapter_id=str(r["chapter_id"]),
                idx=int(r["idx"]),
                source_text=str(r["source_text"]),
                source_hash=str(r["source_hash"]),
                target_text=(
                    str(r["target_text"]) if r["target_text"] is not None else None
                ),
                status=str(r["status"]),
                inline_skeleton=skel,
                host_path=host_path,
                host_part=host_part,
                host_total_parts=host_total,
            )
        )
    return out


class AttachedLoreRow(BaseModel):
    """One row in ``attached_lore`` (PRD §4.3 / F-LB-10 phase 3).

    ``priority`` is a stable, low-first order: 0 is "first to apply",
    higher numbers fall back. ``mode`` is ``read_only`` by default;
    ``writable`` makes the Lore Book a write-back target for new
    auto-proposed entries (the pipeline picks the highest-priority
    writable Lore Book).
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    lore_path: str
    mode: str = schema.AttachedLoreMode.READ_ONLY
    priority: int = 0
    attached_at: int = 0


def list_attached_lore(
    engine_or_conn: Engine | Connection, *, project_id: str
) -> list[AttachedLoreRow]:
    """Return attached Lore Books in priority-ascending, attached-at-tiebreak order."""

    stmt = (
        select(schema.attached_lore)
        .where(schema.attached_lore.c.project_id == project_id)
        .order_by(
            schema.attached_lore.c.priority.asc(),
            schema.attached_lore.c.attached_at.asc(),
        )
    )
    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    return [AttachedLoreRow(**dict(r)) for r in rows]


def attach_lore_book(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    lore_path: str,
    mode: str = schema.AttachedLoreMode.READ_ONLY,
    priority: int | None = None,
) -> AttachedLoreRow:
    """Attach a Lore Book to ``project_id``.

    Idempotent on ``(project_id, lore_path)``: re-attaching an existing
    Lore Book updates its ``mode``/``priority`` instead of failing on
    the unique constraint. ``priority=None`` appends to the end of the
    list (max + 1).
    """

    if mode not in (
        schema.AttachedLoreMode.READ_ONLY,
        schema.AttachedLoreMode.WRITABLE,
    ):
        raise ValueError(f"unknown attached_lore mode: {mode!r}")
    with _begin(engine_or_conn) as conn:
        existing = (
            conn.execute(
                select(schema.attached_lore).where(
                    schema.attached_lore.c.project_id == project_id,
                    schema.attached_lore.c.lore_path == lore_path,
                )
            )
            .mappings()
            .first()
        )
        if existing is not None:
            new_priority = (
                priority if priority is not None else int(existing["priority"])
            )
            conn.execute(
                update(schema.attached_lore)
                .where(schema.attached_lore.c.id == existing["id"])
                .values(mode=mode, priority=new_priority)
            )
            refreshed = (
                conn.execute(
                    select(schema.attached_lore).where(
                        schema.attached_lore.c.id == existing["id"]
                    )
                )
                .mappings()
                .first()
            )
            assert refreshed is not None
            return AttachedLoreRow(**dict(refreshed))

        if priority is None:
            max_row = (
                conn.execute(
                    select(schema.attached_lore.c.priority)
                    .where(schema.attached_lore.c.project_id == project_id)
                    .order_by(schema.attached_lore.c.priority.desc())
                    .limit(1)
                )
                .scalars()
                .first()
            )
            priority = (int(max_row) + 1) if max_row is not None else 0

        row = AttachedLoreRow(
            id=_new_id(),
            project_id=project_id,
            lore_path=lore_path,
            mode=mode,
            priority=int(priority),
            attached_at=_now_unix(),
        )
        conn.execute(insert(schema.attached_lore).values(row.model_dump()))
        append_event(
            conn,
            project_id=project_id,
            kind="lore.attached",
            payload={
                "lore_path": lore_path,
                "mode": mode,
                "priority": int(priority),
            },
        )
        return row


def detach_lore_book(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    lore_path: str,
) -> bool:
    """Detach a Lore Book by its on-disk path. Returns ``True`` if a row was removed."""

    with _begin(engine_or_conn) as conn:
        result = conn.execute(
            delete(schema.attached_lore).where(
                schema.attached_lore.c.project_id == project_id,
                schema.attached_lore.c.lore_path == lore_path,
            )
        )
        removed = (result.rowcount or 0) > 0
        if removed:
            append_event(
                conn,
                project_id=project_id,
                kind="lore.detached",
                payload={"lore_path": lore_path},
            )
        return removed


def update_attached_lore(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    lore_path: str,
    mode: str | None = None,
    priority: int | None = None,
) -> AttachedLoreRow | None:
    """Patch the mode and/or priority of an attached Lore Book row.

    Returns the refreshed row, or ``None`` when the attachment doesn't
    exist. Either ``mode`` or ``priority`` must be supplied — passing
    both ``None`` is a no-op that returns the current row unchanged.
    """

    if mode is None and priority is None:
        # Defer to ``list_attached_lore`` rather than reimplementing
        # the lookup; this preserves the no-op semantics cleanly.
        for row in list_attached_lore(engine_or_conn, project_id=project_id):
            if row.lore_path == lore_path:
                return row
        return None
    if mode is not None and mode not in (
        schema.AttachedLoreMode.READ_ONLY,
        schema.AttachedLoreMode.WRITABLE,
    ):
        raise ValueError(f"unknown attached_lore mode: {mode!r}")
    with _begin(engine_or_conn) as conn:
        values: dict[str, Any] = {}
        if mode is not None:
            values["mode"] = mode
        if priority is not None:
            values["priority"] = int(priority)
        if not values:
            return None
        result = conn.execute(
            update(schema.attached_lore)
            .where(
                schema.attached_lore.c.project_id == project_id,
                schema.attached_lore.c.lore_path == lore_path,
            )
            .values(**values)
        )
        if (result.rowcount or 0) == 0:
            return None
        refreshed = (
            conn.execute(
                select(schema.attached_lore).where(
                    schema.attached_lore.c.project_id == project_id,
                    schema.attached_lore.c.lore_path == lore_path,
                )
            )
            .mappings()
            .first()
        )
        assert refreshed is not None
        append_event(
            conn,
            project_id=project_id,
            kind="lore.attachment_updated",
            payload={
                "lore_path": lore_path,
                "mode": refreshed["mode"],
                "priority": refreshed["priority"],
            },
        )
        return AttachedLoreRow(**dict(refreshed))


class _Begin:
    """Context manager that yields a connection with an active transaction.

    Accepts either an ``Engine`` (begins/commits/rolls back automatically) or
    an existing ``Connection`` (caller owns the transaction lifecycle).
    """

    def __init__(self, engine_or_conn: Engine | Connection) -> None:
        self._engine_or_conn = engine_or_conn
        self._owned: Any = None

    def __enter__(self) -> Connection:
        target = self._engine_or_conn
        if isinstance(target, Engine):
            self._owned = target.begin()
            return self._owned.__enter__()  # type: ignore[no-any-return]
        return target

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._owned is not None:
            self._owned.__exit__(exc_type, exc, tb)


def _begin(engine_or_conn: Engine | Connection) -> _Begin:
    return _Begin(engine_or_conn)


__all__ = [
    "ChapterRow",
    "EventRow",
    "LLMCallRow",
    "MentionCounts",
    "OccurrenceRow",
    "ProjectRow",
    "SegmentRow",
    "append_event",
    "bulk_insert_chapters",
    "bulk_insert_segments",
    "count_mentions_per_entry",
    "create_glossary_entry",
    "create_project",
    "delete_glossary_entry",
    "find_duplicate_source_terms",
    "find_glossary_entry_by_source_term",
    "find_llm_call_by_cache_key",
    "get_glossary_entry",
    "get_llm_overrides",
    "get_project",
    "get_segment",
    "insert_llm_call",
    "list_aliases",
    "list_chapters",
    "list_events",
    "list_glossary_entries",
    "list_glossary_revisions",
    "list_llm_calls",
    "list_mentions",
    "list_occurrences",
    "list_projects",
    "list_segments",
    "list_segments_by_status",
    "list_segments_for_project",
    "merge_glossary_entries",
    "record_mentions",
    "rewrite_segment_source",
    "segment_row_from",
    "segment_row_to",
    "set_aliases",
    "set_llm_overrides",
    "update_glossary_entry",
    "update_project_budget",
    "update_project_name",
    "update_project_style",
    "update_segment_status",
    "update_segment_translation",
]
