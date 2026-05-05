"""project.context_max_segments + context_max_chars columns

Revision ID: 0009_project_context_defaults
Revises: 0008_intake_run
Create Date: 2026-05-04

Per-project defaults for the translator's preceding-segment context
window (PRD §8.1 follow-up). Today the BatchModal and CLI both expose
these knobs per-run but the Reader's chapter-batch / single-segment
translate paths can't reach them, and curators have to retype the
same values for every batch on the same book. Promoting them to the
project row lets the Settings → Project tab persist a sensible
default per project, and lets the Reader honor it without dragging
the BatchModal in.

Both columns default to ``0`` so existing rows behave exactly as
before (no preceding-segment context is included).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_project_context_defaults"
down_revision: str | Sequence[str] | None = "0008_intake_run"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch:
        batch.add_column(
            sa.Column(
                "context_max_segments",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "context_max_chars",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("project") as batch:
        batch.drop_column("context_max_chars")
        batch.drop_column("context_max_segments")
