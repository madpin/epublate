"""relax glossary_entry.source_term + add source_known (Lore Books / M6)

Revision ID: 0005_glossary_target_only
Revises: 0004_project_llm_overrides
Create Date: 2026-05-02

Phase 2 of the Lore Books rollout (PRD §4.3 / F-LB-3): a Lore Book
must be able to host *target-only* glossary entries — proper nouns
whose canonical target form is fixed (e.g. extracted from an
already-translated book in a series) but whose source spelling we
don't know yet. The translator picks up the source mapping
on-the-fly when it encounters the entity.

Schema changes:

* ``glossary_entry.source_term`` becomes nullable.
* New column ``glossary_entry.source_known`` (boolean, default 1)
  flags whether a row was authored with a source term. The Phase 3
  validator will use this to switch ``locked`` rows from a hard fail
  to a soft warning (F-LB-9) when ``source_known=0``.

Project-scoped DBs keep the invariant that ``source_term`` is set
(every existing row gets ``source_known=1``); the relaxation is
specifically for the Lore Book DB shape that lands later in this
phase.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_glossary_target_only"
down_revision: str | Sequence[str] | None = "0004_project_llm_overrides"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("glossary_entry") as batch:
        batch.alter_column(
            "source_term",
            existing_type=sa.Text(),
            nullable=True,
        )
        batch.add_column(
            sa.Column(
                "source_known",
                sa.Integer(),
                nullable=False,
                server_default="1",
            )
        )
    # Defensive backfill: every existing row predates the column, so
    # explicitly mark them as having a known source term. The
    # ``server_default`` covers fresh inserts — this UPDATE handles
    # rows that already exist when the migration runs.
    op.execute("UPDATE glossary_entry SET source_known = 1")


def downgrade() -> None:
    # Re-tightening ``source_term`` is destructive when target-only
    # rows already exist; we drop them rather than silently lose the
    # canonical target form to a NOT NULL violation.
    op.execute("DELETE FROM glossary_entry WHERE source_term IS NULL")
    with op.batch_alter_table("glossary_entry") as batch:
        batch.alter_column(
            "source_term",
            existing_type=sa.Text(),
            nullable=False,
        )
        batch.drop_column("source_known")
