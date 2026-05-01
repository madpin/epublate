"""SQLite schema (PRD §6.4) declared in SQLAlchemy Core.

All tables are declared here so that Alembic's ``--autogenerate`` has a single
source of truth, and so later milestones add migrations rather than retro-fit
the bootstrap. Constants for column statuses are kept near the schema for
discoverability.

Hard rules (db-and-persistence rule):

* Use parameterized queries only — never f-string SQL.
* SQLite is opened in WAL mode by :func:`epublate.db.connect`.
* Migrations live in ``epublate.db.migrations``; do not hand-edit
  applied revisions.
"""

from __future__ import annotations

from sqlalchemy import (
    BLOB,
    Column,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    UniqueConstraint,
)

# A predictable naming convention keeps Alembic autogeneration stable across
# SQLite (no native FK names by default) and other backends.
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
}

metadata = MetaData(naming_convention=NAMING_CONVENTION)


# Status enums kept as constants; the validator and prompt builders import
# from here so a string typo can't drift between layers.
class ChapterStatus:
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    LOCKED = "locked"


class SegmentStatus:
    PENDING = "pending"
    TRANSLATED = "translated"
    VALIDATED = "validated"
    FLAGGED = "flagged"
    APPROVED = "approved"


class GlossaryStatus:
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    LOCKED = "locked"


project = Table(
    "project",
    metadata,
    Column("id", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("source_lang", Text, nullable=False),
    Column("target_lang", Text, nullable=False),
    Column("source_path", Text, nullable=False),
    Column("style_guide", Text, nullable=True),
    Column("style_profile", Text, nullable=True),
    Column("budget_usd", Float, nullable=True),
    Column("created_at", Integer, nullable=False),
)

chapter = Table(
    "chapter",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "project_id",
        Text,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("spine_idx", Integer, nullable=False),
    Column("href", Text, nullable=False),
    Column("title", Text, nullable=True),
    Column("status", Text, nullable=False),
    UniqueConstraint("project_id", "spine_idx", name="chapter_project_spine"),
)

segment = Table(
    "segment",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "chapter_id",
        Text,
        ForeignKey("chapter.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("idx", Integer, nullable=False),
    Column("source_text", Text, nullable=False),
    Column("source_hash", Text, nullable=False),
    Column("target_text", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("inline_skeleton", BLOB, nullable=True),
    UniqueConstraint("chapter_id", "idx", name="segment_chapter_idx"),
)

glossary_entry = Table(
    "glossary_entry",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "project_id",
        Text,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("type", Text, nullable=False),
    Column("source_term", Text, nullable=False),
    Column("target_term", Text, nullable=False),
    Column("gender", Text, nullable=True),
    Column("status", Text, nullable=False),
    Column("notes", Text, nullable=True),
    Column(
        "first_seen_segment_id",
        Text,
        ForeignKey("segment.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
)

glossary_alias = Table(
    "glossary_alias",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "entry_id",
        Text,
        ForeignKey("glossary_entry.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("side", Text, nullable=False),
    Column("text", Text, nullable=False),
    UniqueConstraint("entry_id", "side", "text", name="alias_unique"),
)

glossary_revision = Table(
    "glossary_revision",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "entry_id",
        Text,
        ForeignKey("glossary_entry.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("prev_target_term", Text, nullable=True),
    Column("new_target_term", Text, nullable=True),
    Column("reason", Text, nullable=True),
    Column("created_at", Integer, nullable=False),
)

entity_mention = Table(
    "entity_mention",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "segment_id",
        Text,
        ForeignKey("segment.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "entry_id",
        Text,
        ForeignKey("glossary_entry.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("source_span_start", Integer, nullable=True),
    Column("source_span_end", Integer, nullable=True),
)

llm_call = Table(
    "llm_call",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "project_id",
        Text,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column(
        "segment_id",
        Text,
        ForeignKey("segment.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("purpose", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("prompt_tokens", Integer, nullable=True),
    Column("completion_tokens", Integer, nullable=True),
    Column("cost_usd", Float, nullable=True),
    Column("cache_hit", Integer, nullable=False, default=0),
    Column("cache_key", Text, nullable=True),
    Column("request_json", Text, nullable=True),
    Column("response_json", Text, nullable=True),
    Column("created_at", Integer, nullable=False),
)

Index(
    "ix_llm_call_project_cache_key",
    llm_call.c.project_id,
    llm_call.c.cache_key,
)

embedding = Table(
    "embedding",
    metadata,
    Column("id", Text, primary_key=True),
    Column("scope", Text, nullable=False),
    Column("ref_id", Text, nullable=False),
    Column("model", Text, nullable=False),
    Column("dim", Integer, nullable=False),
    Column("vector", BLOB, nullable=False),
)

event = Table(
    "event",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("project_id", Text, nullable=False),
    Column("ts", Integer, nullable=False),
    Column("kind", Text, nullable=False),
    Column("payload_json", Text, nullable=False),
)


ALL_TABLES: tuple[Table, ...] = (
    project,
    chapter,
    segment,
    glossary_entry,
    glossary_alias,
    glossary_revision,
    entity_mention,
    llm_call,
    embedding,
    event,
)


__all__ = [
    "ALL_TABLES",
    "NAMING_CONVENTION",
    "ChapterStatus",
    "GlossaryStatus",
    "SegmentStatus",
    "chapter",
    "embedding",
    "entity_mention",
    "event",
    "glossary_alias",
    "glossary_entry",
    "glossary_revision",
    "llm_call",
    "metadata",
    "project",
    "segment",
]
