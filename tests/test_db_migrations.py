"""Smoke + roundtrip tests for the new migrations 0008 / 0009.

Migrations live in ``src/epublate/db/migrations/versions``; the
:func:`epublate.db.connect` fixture already runs them in order, so the
expected-tables / expected-columns assertions below double as a
guard against accidentally dropping a migration in a future PR.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from epublate.db import apply_migrations, connect


def _columns(engine: Engine, table_name: str) -> set[str]:
    inspector = inspect(engine)
    return {c["name"] for c in inspector.get_columns(table_name)}


def _tables(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).all()
    return {r[0] for r in rows}


def test_intake_run_table_created(project_db: Engine) -> None:
    """0008 lands the ``intake_run`` table with every PRD column."""

    assert "intake_run" in _tables(project_db)
    cols = _columns(project_db, "intake_run")
    expected = {
        "id",
        "project_id",
        "kind",
        "chapter_id",
        "helper_model",
        "started_at",
        "finished_at",
        "status",
        "chunks",
        "cached_chunks",
        "proposed_count",
        "failed_chunks",
        "prompt_tokens",
        "completion_tokens",
        "cost_usd",
        "pov",
        "tense",
        "register",
        "audience",
        "suggested_style_profile",
        "notes",
        "curator_notes",
        "error",
    }
    assert expected <= cols, f"missing columns: {expected - cols}"


def test_intake_run_entry_table_created(project_db: Engine) -> None:
    """0008 also creates the join table linking runs to entries."""

    assert "intake_run_entry" in _tables(project_db)
    cols = _columns(project_db, "intake_run_entry")
    assert {"intake_run_id", "entry_id", "created_at"} <= cols


def test_project_context_columns_added(project_db: Engine) -> None:
    """0009 adds ``context_max_segments`` / ``context_max_chars`` to ``project``."""

    cols = _columns(project_db, "project")
    assert "context_max_segments" in cols
    assert "context_max_chars" in cols


def test_apply_migrations_idempotent_with_intake(tmp_path: Path) -> None:
    """Re-running migrations on a fresh DB is still a no-op."""

    db_path = tmp_path / "twice.epublate"
    engine = connect(db_path)
    try:
        apply_migrations(engine)  # second run is a no-op
        tables = _tables(engine)
        assert {"intake_run", "intake_run_entry"} <= tables
        assert {"context_max_segments", "context_max_chars"} <= _columns(
            engine, "project"
        )
    finally:
        engine.dispose()
