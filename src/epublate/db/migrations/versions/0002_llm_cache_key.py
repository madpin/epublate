"""add llm_call.cache_key + composite index (PRD F-LLM-6 / M2)

Revision ID: 0002_llm_cache_key
Revises: 0001_initial
Create Date: 2026-04-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_llm_cache_key"
down_revision: str | Sequence[str] | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("llm_call") as batch:
        batch.add_column(sa.Column("cache_key", sa.Text(), nullable=True))
    op.create_index(
        "ix_llm_call_project_cache_key",
        "llm_call",
        ["project_id", "cache_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_call_project_cache_key", table_name="llm_call")
    with op.batch_alter_table("llm_call") as batch:
        batch.drop_column("cache_key")
