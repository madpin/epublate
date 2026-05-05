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
    # Per-project overrides for LLM endpoint / models. Stored as a JSON
    # blob (see ``epublate.db.repo.set_llm_overrides``) so we don't
    # churn the schema for each new knob the Settings panel exposes.
    Column("llm_overrides", Text, nullable=True),
    Column("created_at", Integer, nullable=False),
    # Discriminator between regular translation projects (``"book"``)
    # and Lore Book projects (``"lore"``). The Lore Book carries its
    # own glossary tables — same FKs, different lifecycle (no
    # chapters/segments). See PRD F-LB-10.
    Column(
        "kind",
        Text,
        nullable=False,
        server_default="book",
    ),
    # Per-project defaults for the translator's preceding-segment
    # context (PRD §8.1 follow-up): the BatchModal pre-fills from
    # these and the Reader's chapter/single-segment translate paths
    # honor them automatically. ``0`` means "no context", so legacy
    # rows backfilled to the default behave exactly like before.
    Column(
        "context_max_segments",
        Integer,
        nullable=False,
        server_default="0",
    ),
    Column(
        "context_max_chars",
        Integer,
        nullable=False,
        server_default="0",
    ),
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
    # ``source_term`` is nullable to accommodate target-only Lore Book
    # entries (PRD F-LB-3 / Phase 3): a curator can lock the canonical
    # *target* form for a proper noun without yet knowing its source-side
    # spelling, and the translator picks up the source mapping on the fly.
    # Project-scoped entries continue to require a source term — the
    # repo layer enforces that boundary.
    Column("source_term", Text, nullable=True),
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
    # ``source_known`` defaults to true; the migration backfills every
    # existing row to true so legacy projects keep behaving identically.
    # Setting it false implies the entry is target-only and the
    # validator should treat the locked status as a *soft* lock
    # (warn-only) per F-LB-9.
    Column(
        "source_known",
        Integer,
        nullable=False,
        server_default="1",
    ),
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


# Lore Book extension tables — coexist with the project schema so a
# Lore Book DB and a translation-project DB share the same migrations.
# A regular translation project never inserts rows here.
lore_meta = Table(
    "lore_meta",
    metadata,
    Column(
        "project_id",
        Text,
        ForeignKey("project.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("description", Text, nullable=True),
    Column("schema_version", Integer, nullable=False, server_default="1"),
    Column(
        "default_proposal_kind",
        Text,
        nullable=False,
        server_default="target",
    ),
    Column("created_at", Integer, nullable=False),
    Column("updated_at", Integer, nullable=False),
)


lore_source = Table(
    "lore_source",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "project_id",
        Text,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("kind", Text, nullable=False),
    Column("epub_path", Text, nullable=False),
    Column("status", Text, nullable=False, server_default="ingested"),
    Column("entries_added", Integer, nullable=False, server_default="0"),
    Column("notes", Text, nullable=True),
    Column("ingested_at", Integer, nullable=False),
)

Index(
    "ix_lore_source_project_id",
    lore_source.c.project_id,
)


# ``attached_lore`` rows live in a *project* DB and point at on-disk
# Lore Book directories (PRD §4.3 / F-LB-10 phase 3). The pipeline
# reads them at translate-time to merge in canonical proper-noun rules,
# and write-backs are routed to the highest-priority writable Lore Book.
# Lower ``priority`` means the entry "wins" earlier when merging — this
# matches the user's mental model ("first in the list = most authoritative").
attached_lore = Table(
    "attached_lore",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "project_id",
        Text,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("lore_path", Text, nullable=False),
    Column("mode", Text, nullable=False, server_default="read_only"),
    Column("priority", Integer, nullable=False, server_default="0"),
    Column("attached_at", Integer, nullable=False),
    UniqueConstraint("project_id", "lore_path", name="attached_lore_project_path"),
)

Index(
    "ix_attached_lore_project_id",
    attached_lore.c.project_id,
)


class AttachedLoreMode:
    """``attached_lore.mode`` enum (PRD §4.3 / F-LB-10 phase 3).

    Read-only attachments contribute glossary entries to the merged
    pipeline view but never receive new proposals. Writable attachments
    additionally accept auto-proposed entries from the helper pre-pass
    so the Lore Book accumulates lore across an entire series.
    """

    READ_ONLY = "read_only"
    WRITABLE = "writable"


class LoreSourceKind:
    """``lore_source.kind`` enum (PRD F-LB-10)."""

    SOURCE = "source"
    TARGET = "target"


class LoreSourceStatus:
    """``lore_source.status`` enum."""

    INGESTED = "ingested"
    FAILED = "failed"


class ProjectKind:
    """``project.kind`` enum.

    A regular translation project is ``BOOK``; a Lore Book project
    carries the same glossary schema but no chapters / segments and
    uses :data:`LORE` to mark itself.
    """

    BOOK = "book"
    LORE = "lore"


# Persistent record of every helper-LLM intake/pre-pass invocation
# (PRD §4.3 / §7.1). The append-only ``event`` table keeps a terse
# audit trail; this table is the *editable* surface — curators read
# and annotate the rich payload (POV, tense, helper notes, suggested
# style profile, link back to the proposed glossary entries) from
# the Intake history screen.
intake_run = Table(
    "intake_run",
    metadata,
    Column("id", Text, primary_key=True),
    Column(
        "project_id",
        Text,
        ForeignKey("project.id", ondelete="CASCADE"),
        nullable=False,
    ),
    # ``book_intake`` for the one-shot ``run_book_intake`` path
    # (Dashboard ``e``); ``chapter_pre_pass`` for the per-chapter
    # ``run_pre_pass`` invocations the batch worker fires.
    Column("kind", Text, nullable=False),
    # Only set for ``chapter_pre_pass`` runs; ``book_intake`` spans
    # the whole spine and leaves this NULL.
    Column(
        "chapter_id",
        Text,
        ForeignKey("chapter.id", ondelete="SET NULL"),
        nullable=True,
    ),
    Column("helper_model", Text, nullable=False),
    Column("started_at", Integer, nullable=False),
    Column("finished_at", Integer, nullable=False),
    # ``completed`` / ``cancelled`` / ``aborted`` / ``rate_limited`` /
    # ``failed`` — keeps the status clean from the audit-event kind.
    Column("status", Text, nullable=False),
    Column("chunks", Integer, nullable=False, server_default="0"),
    Column("cached_chunks", Integer, nullable=False, server_default="0"),
    Column("proposed_count", Integer, nullable=False, server_default="0"),
    Column("failed_chunks", Integer, nullable=False, server_default="0"),
    Column("prompt_tokens", Integer, nullable=False, server_default="0"),
    Column("completion_tokens", Integer, nullable=False, server_default="0"),
    Column("cost_usd", Float, nullable=False, server_default="0"),
    Column("pov", Text, nullable=True),
    Column("tense", Text, nullable=True),
    Column("register", Text, nullable=True),
    Column("audience", Text, nullable=True),
    Column("suggested_style_profile", Text, nullable=True),
    # Helper-LLM notes captured verbatim (JSON-serialized list of
    # strings) so the curator sees exactly what the helper wrote.
    Column("notes", Text, nullable=True),
    # Free-form curator annotation. The screen lets the curator
    # record "rejected this run's POV", "ignore: chapter is metadata",
    # etc., without modifying the helper-emitted payload.
    Column("curator_notes", Text, nullable=True),
    Column("error", Text, nullable=True),
)


Index(
    "ix_intake_run_project_id",
    intake_run.c.project_id,
)


# Join table linking a single ``intake_run`` to the proposed glossary
# entries it surfaced. Composite PK keeps duplicates impossible; the
# ON DELETE CASCADE clauses mean the link disappears as soon as
# either parent is deleted (the entry might be merged away during
# curation).
intake_run_entry = Table(
    "intake_run_entry",
    metadata,
    Column(
        "intake_run_id",
        Text,
        ForeignKey("intake_run.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column(
        "entry_id",
        Text,
        ForeignKey("glossary_entry.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("created_at", Integer, nullable=False),
)


class IntakeRunKind:
    """``intake_run.kind`` enum."""

    BOOK_INTAKE = "book_intake"
    CHAPTER_PRE_PASS = "chapter_pre_pass"


class IntakeRunStatus:
    """``intake_run.status`` enum.

    Mirrors the terminal-event vocabulary already emitted by
    :func:`epublate.core.extractor.run_book_intake` /
    :func:`run_pre_pass` so the screen can map "events feed says
    aborted" to "intake_run row says aborted" without a translation
    table.
    """

    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ABORTED = "aborted"
    RATE_LIMITED = "rate_limited"
    FAILED = "failed"


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
    lore_meta,
    lore_source,
    attached_lore,
    intake_run,
    intake_run_entry,
)


__all__ = [
    "ALL_TABLES",
    "NAMING_CONVENTION",
    "AttachedLoreMode",
    "ChapterStatus",
    "GlossaryStatus",
    "IntakeRunKind",
    "IntakeRunStatus",
    "LoreSourceKind",
    "LoreSourceStatus",
    "ProjectKind",
    "SegmentStatus",
    "attached_lore",
    "chapter",
    "embedding",
    "entity_mention",
    "event",
    "glossary_alias",
    "glossary_entry",
    "glossary_revision",
    "intake_run",
    "intake_run_entry",
    "llm_call",
    "lore_meta",
    "lore_source",
    "metadata",
    "project",
    "segment",
]
