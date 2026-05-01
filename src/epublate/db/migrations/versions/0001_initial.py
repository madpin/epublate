"""initial schema (PRD §6.4)

Revision ID: 0001_initial
Revises:
Create Date: 2026-04-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_lang", sa.Text(), nullable=False),
        sa.Column("target_lang", sa.Text(), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("style_guide", sa.Text(), nullable=True),
        sa.Column("budget_usd", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )

    op.create_table(
        "chapter",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("spine_idx", sa.Integer(), nullable=False),
        sa.Column("href", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.UniqueConstraint("project_id", "spine_idx", name="chapter_project_spine"),
    )

    op.create_table(
        "segment",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "chapter_id",
            sa.Text(),
            sa.ForeignKey("chapter.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("idx", sa.Integer(), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("target_text", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("inline_skeleton", sa.LargeBinary(), nullable=True),
        sa.UniqueConstraint("chapter_id", "idx", name="segment_chapter_idx"),
    )

    op.create_table(
        "glossary_entry",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("source_term", sa.Text(), nullable=False),
        sa.Column("target_term", sa.Text(), nullable=False),
        sa.Column("gender", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "first_seen_segment_id",
            sa.Text(),
            sa.ForeignKey("segment.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
    )

    op.create_table(
        "glossary_alias",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "entry_id",
            sa.Text(),
            sa.ForeignKey("glossary_entry.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("side", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.UniqueConstraint("entry_id", "side", "text", name="alias_unique"),
    )

    op.create_table(
        "glossary_revision",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "entry_id",
            sa.Text(),
            sa.ForeignKey("glossary_entry.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("prev_target_term", sa.Text(), nullable=True),
        sa.Column("new_target_term", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )

    op.create_table(
        "entity_mention",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "segment_id",
            sa.Text(),
            sa.ForeignKey("segment.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "entry_id",
            sa.Text(),
            sa.ForeignKey("glossary_entry.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_span_start", sa.Integer(), nullable=True),
        sa.Column("source_span_end", sa.Integer(), nullable=True),
    )

    op.create_table(
        "llm_call",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "project_id",
            sa.Text(),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "segment_id",
            sa.Text(),
            sa.ForeignKey("segment.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column(
            "cache_hit", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("request_json", sa.Text(), nullable=True),
        sa.Column("response_json", sa.Text(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
    )

    op.create_table(
        "embedding",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("ref_id", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("vector", sa.LargeBinary(), nullable=False),
    )

    op.create_table(
        "event",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("ts", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("event")
    op.drop_table("embedding")
    op.drop_table("llm_call")
    op.drop_table("entity_mention")
    op.drop_table("glossary_revision")
    op.drop_table("glossary_alias")
    op.drop_table("glossary_entry")
    op.drop_table("segment")
    op.drop_table("chapter")
    op.drop_table("project")
