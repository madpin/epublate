"""Lore-Book-scoped repo helpers (PRD §4.3 / F-LB-10).

Wraps the ``lore_meta`` and ``lore_source`` tables. The actual
glossary entries live in the same ``glossary_entry`` /
``glossary_alias`` / ``glossary_revision`` tables that translation
projects use, so the existing :mod:`epublate.db.repo` helpers (and
:mod:`epublate.glossary.io`) keep working unmodified.

Keep this file *thin*: only the Lore-Book-specific tables go here.
Anything that touches ``glossary_entry`` should live in
:mod:`epublate.db.repo` (so it stays usable for translation projects)
or in :mod:`epublate.glossary.io` (for import/export logic).
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict
from sqlalchemy import insert, select, update
from sqlalchemy.engine import Connection, Engine

from epublate.db import schema
from epublate.db.repo import _begin


class LoreMetaRow(BaseModel):
    """Lore-Book-only metadata (description, defaults).

    The 1-1 relation with ``project`` mirrors how
    :class:`epublate.db.repo.ProjectRow` carries shared fields. We
    only land here the bits that don't make sense for translation
    projects (description text, default proposal kind).
    """

    model_config = ConfigDict(extra="forbid")

    project_id: str
    description: str | None = None
    schema_version: int = 1
    default_proposal_kind: str = schema.LoreSourceKind.TARGET
    created_at: int = 0
    updated_at: int = 0


class LoreSourceRow(BaseModel):
    """One ingested ePub recorded against a Lore Book (PRD F-LB-10)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    project_id: str
    kind: str  # ``LoreSourceKind.SOURCE`` or ``LoreSourceKind.TARGET``
    epub_path: str
    status: str = schema.LoreSourceStatus.INGESTED
    entries_added: int = 0
    notes: str | None = None
    ingested_at: int = 0


def _now_unix() -> int:
    return int(time.time())


def _new_id() -> str:
    return uuid.uuid4().hex


def create_lore_meta(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    description: str | None = None,
    default_proposal_kind: str = schema.LoreSourceKind.TARGET,
    schema_version: int = 1,
    created_at: int | None = None,
) -> LoreMetaRow:
    """Insert the per-Lore-Book metadata row."""

    now = created_at or _now_unix()
    row = LoreMetaRow(
        project_id=project_id,
        description=description,
        schema_version=schema_version,
        default_proposal_kind=default_proposal_kind,
        created_at=now,
        updated_at=now,
    )
    stmt = insert(schema.lore_meta).values(**row.model_dump())
    with _begin(engine_or_conn) as conn:
        conn.execute(stmt)
    return row


def get_lore_meta(
    engine_or_conn: Engine | Connection, *, project_id: str
) -> LoreMetaRow | None:
    stmt = select(schema.lore_meta).where(schema.lore_meta.c.project_id == project_id)
    with _begin(engine_or_conn) as conn:
        row = conn.execute(stmt).mappings().first()
    if row is None:
        return None
    return LoreMetaRow(**dict(row))


def update_lore_meta(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    description: str | None = None,
    default_proposal_kind: str | None = None,
) -> LoreMetaRow:
    """Patch ``description`` / ``default_proposal_kind`` for a Lore Book."""

    values: dict[str, Any] = {"updated_at": _now_unix()}
    if description is not None:
        values["description"] = description
    if default_proposal_kind is not None:
        values["default_proposal_kind"] = default_proposal_kind
    with _begin(engine_or_conn) as conn:
        existing = (
            conn.execute(
                select(schema.lore_meta).where(
                    schema.lore_meta.c.project_id == project_id
                )
            )
            .mappings()
            .first()
        )
        if existing is None:
            raise ValueError(f"lore_meta not found for project {project_id}")
        if len(values) > 1:
            conn.execute(
                update(schema.lore_meta)
                .where(schema.lore_meta.c.project_id == project_id)
                .values(**values)
            )
        refreshed = (
            conn.execute(
                select(schema.lore_meta).where(
                    schema.lore_meta.c.project_id == project_id
                )
            )
            .mappings()
            .first()
        )
    assert refreshed is not None
    return LoreMetaRow(**dict(refreshed))


def insert_lore_source(
    engine_or_conn: Engine | Connection,
    *,
    project_id: str,
    kind: str,
    epub_path: str,
    status: str = schema.LoreSourceStatus.INGESTED,
    entries_added: int = 0,
    notes: str | None = None,
    source_id: str | None = None,
    ingested_at: int | None = None,
) -> LoreSourceRow:
    """Record an ingested ePub against a Lore Book.

    Pure book-keeping: this does *not* run the extractor. The lore
    ingest helpers call the extractor first and then call this with
    the resulting ``entries_added`` count.
    """

    if kind not in (schema.LoreSourceKind.SOURCE, schema.LoreSourceKind.TARGET):
        raise ValueError(f"lore_source.kind must be 'source' or 'target', got {kind!r}")
    row = LoreSourceRow(
        id=source_id or _new_id(),
        project_id=project_id,
        kind=kind,
        epub_path=epub_path,
        status=status,
        entries_added=entries_added,
        notes=notes,
        ingested_at=ingested_at or _now_unix(),
    )
    stmt = insert(schema.lore_source).values(**row.model_dump())
    with _begin(engine_or_conn) as conn:
        conn.execute(stmt)
    return row


def list_lore_sources(
    engine_or_conn: Engine | Connection, *, project_id: str
) -> list[LoreSourceRow]:
    stmt = (
        select(schema.lore_source)
        .where(schema.lore_source.c.project_id == project_id)
        .order_by(schema.lore_source.c.ingested_at.desc())
    )
    with _begin(engine_or_conn) as conn:
        rows = conn.execute(stmt).mappings().all()
    return [LoreSourceRow(**dict(r)) for r in rows]


__all__ = [
    "LoreMetaRow",
    "LoreSourceRow",
    "create_lore_meta",
    "get_lore_meta",
    "insert_lore_source",
    "list_lore_sources",
    "update_lore_meta",
]
