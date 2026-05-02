"""add project.llm_overrides JSON column (Settings overhaul / M6)

Revision ID: 0004_project_llm_overrides
Revises: 0003_project_style_profile
Create Date: 2026-05-02

The Settings screen needs per-project overrides for LLM endpoint /
translator-model / helper-model so a curator can pin a specific model
to a book without exporting environment variables every session. We
store the overrides as a single JSON blob on the ``project`` row to
avoid schema churn whenever a new override key shows up.

The API key intentionally stays env-only — putting it in a project DB
would invite accidental commits of secrets into a `.epublate` archive.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_project_llm_overrides"
down_revision: str | Sequence[str] | None = "0003_project_style_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch:
        batch.add_column(sa.Column("llm_overrides", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("project") as batch:
        batch.drop_column("llm_overrides")
