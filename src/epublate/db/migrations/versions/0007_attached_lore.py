"""attached_lore table — Lore Book ↔ project links (Phase 3)

Revision ID: 0007_attached_lore
Revises: 0006_lore_book_schema
Create Date: 2026-05-02

Phase 3 of the Lore Books rollout (PRD §4.3 / F-LB-10): now that
Lore Books exist as a stand-alone artifact (migration 0006), translation
projects need a way to *attach* one or more of them so the pipeline can
merge their canonical proper-noun rules into every translate call. This
migration adds the join table that lives in the **project** DB and
points at on-disk Lore Book directories by absolute path.

Why a path and not a foreign key: a Lore Book is its own SQLite DB on
disk, often in the user's library (``~/.config/epublate/lore``), so
there's no cross-DB referential integrity to lean on. The pipeline is
expected to revalidate the path before opening (and surface a friendly
error if it disappears) — see ``epublate.lore.LoreBook.open``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_attached_lore"
down_revision: str | Sequence[str] | None = "0006_lore_book_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "attached_lore",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("lore_path", sa.Text(), nullable=False),
        sa.Column(
            "mode",
            sa.Text(),
            nullable=False,
            server_default="read_only",
        ),
        sa.Column(
            "priority",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("attached_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
            name="fk_attached_lore_project_id_project",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_attached_lore"),
        sa.UniqueConstraint(
            "project_id",
            "lore_path",
            name="attached_lore_project_path",
        ),
    )
    op.create_index(
        "ix_attached_lore_project_id",
        "attached_lore",
        ["project_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_attached_lore_project_id", table_name="attached_lore")
    op.drop_table("attached_lore")
