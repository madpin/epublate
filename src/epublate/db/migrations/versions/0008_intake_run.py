"""intake_run + intake_run_entry tables (persistent intake records)

Revision ID: 0008_intake_run
Revises: 0007_attached_lore
Create Date: 2026-05-04

Persists every helper-LLM intake / per-chapter pre-pass invocation as
a first-class row the curator can browse and annotate from the
Intake history screen (PRD §4.3 / §7.1). Today's flow records the
terminal events into the append-only ``event`` table but discards the
rich roll-up (notes, audience, list of proposed entries, suggested
style profile); :class:`epublate.db.schema.intake_run` now owns that
state in an editable table while ``event`` stays as the audit trail.

The ``intake_run_entry`` join table links each run to the proposed
glossary entries it surfaced so the screen can render
"this pre-pass added: X, Y, Z" without re-traversing per-segment
``entity.proposed`` events.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_intake_run"
down_revision: str | Sequence[str] | None = "0007_attached_lore"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intake_run",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("project_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("chapter_id", sa.Text(), nullable=True),
        sa.Column("helper_model", sa.Text(), nullable=False),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cached_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("proposed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("prompt_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "completion_tokens", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("cost_usd", sa.Float(), nullable=False, server_default="0"),
        sa.Column("pov", sa.Text(), nullable=True),
        sa.Column("tense", sa.Text(), nullable=True),
        sa.Column("register", sa.Text(), nullable=True),
        sa.Column("audience", sa.Text(), nullable=True),
        sa.Column("suggested_style_profile", sa.Text(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("curator_notes", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
            name="fk_intake_run_project_id_project",
        ),
        sa.ForeignKeyConstraint(
            ["chapter_id"],
            ["chapter.id"],
            ondelete="SET NULL",
            name="fk_intake_run_chapter_id_chapter",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_intake_run"),
    )
    op.create_index(
        "ix_intake_run_project_id",
        "intake_run",
        ["project_id"],
        unique=False,
    )

    op.create_table(
        "intake_run_entry",
        sa.Column("intake_run_id", sa.Text(), nullable=False),
        sa.Column("entry_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["intake_run_id"],
            ["intake_run.id"],
            ondelete="CASCADE",
            name="fk_intake_run_entry_intake_run_id_intake_run",
        ),
        sa.ForeignKeyConstraint(
            ["entry_id"],
            ["glossary_entry.id"],
            ondelete="CASCADE",
            name="fk_intake_run_entry_entry_id_glossary_entry",
        ),
        sa.PrimaryKeyConstraint(
            "intake_run_id",
            "entry_id",
            name="pk_intake_run_entry",
        ),
    )


def downgrade() -> None:
    op.drop_table("intake_run_entry")
    op.drop_index("ix_intake_run_project_id", table_name="intake_run")
    op.drop_table("intake_run")
