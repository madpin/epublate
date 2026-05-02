"""project.kind + lore_meta + lore_source tables (Lore Books / M6)

Revision ID: 0006_lore_book_schema
Revises: 0005_glossary_target_only
Create Date: 2026-05-02

Phase 2 of the Lore Books rollout (PRD §4.3 / F-LB-10): a *Lore Book*
is a portable artifact that hosts a glossary independent of any one
translation project. It lives in its own SQLite DB so it can be
attached to many projects (e.g. the books in a series) without
duplicating its entries.

Storage strategy: a Lore Book reuses the existing ``glossary_entry`` /
``glossary_alias`` / ``glossary_revision`` tables (the FK column
``project_id`` is repurposed as the Lore Book's identifier — a single
row in ``project`` with ``kind='lore'`` anchors the FK chain). The
schema additions in this migration are:

* ``project.kind`` — discriminates regular books (``'book'``) from
  Lore Books (``'lore'``). Defaulting to ``'book'`` keeps existing
  rows working.
* ``lore_meta`` — Lore-Book-specific knobs (free-form description,
  schema version, default proposal kind). One row per Lore Book.
* ``lore_source`` — per-Lore-Book audit trail of every ePub ingested,
  whether as a source-language scan or a target-language extraction.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_lore_book_schema"
down_revision: str | Sequence[str] | None = "0005_glossary_target_only"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch:
        batch.add_column(
            sa.Column(
                "kind",
                sa.Text(),
                nullable=False,
                server_default="book",
            )
        )
    op.execute("UPDATE project SET kind = 'book' WHERE kind IS NULL")

    op.create_table(
        "lore_meta",
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "default_proposal_kind",
            sa.Text(),
            nullable=False,
            server_default="target",
        ),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
            name="fk_lore_meta_project_id_project",
        ),
        sa.PrimaryKeyConstraint("project_id", name="pk_lore_meta"),
    )

    op.create_table(
        "lore_source",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("epub_path", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="ingested"),
        sa.Column("entries_added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("ingested_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
            name="fk_lore_source_project_id_project",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_lore_source"),
    )
    op.create_index(
        "ix_lore_source_project_id",
        "lore_source",
        ["project_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_lore_source_project_id", table_name="lore_source")
    op.drop_table("lore_source")
    op.drop_table("lore_meta")
    with op.batch_alter_table("project") as batch:
        batch.drop_column("kind")
