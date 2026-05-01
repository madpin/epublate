"""add project.style_profile (PRD F-STYLE-1)

Revision ID: 0003_project_style_profile
Revises: 0002_llm_cache_key
Create Date: 2026-04-30

The project row already has ``style_guide TEXT NULL`` for free-form prompt
text; this revision adds a sibling ``style_profile TEXT NULL`` column that
records *which preset the curator chose* (e.g. ``"young_adult"``). Both
columns coexist deliberately:

* ``style_profile`` is the slug — short, machine-readable, suitable for the
  dashboard / settings panel labels.
* ``style_guide`` is the resolved prompt block fed to the translator.

Older projects keep ``NULL`` in both columns and the translator's prompt
block is omitted (status quo). Newly created projects default to the
literary-fiction preset.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_project_style_profile"
down_revision: str | Sequence[str] | None = "0002_llm_cache_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch:
        batch.add_column(sa.Column("style_profile", sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("project") as batch:
        batch.drop_column("style_profile")
