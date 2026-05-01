"""DB schema, WAL invariant, and basic CRUD round-trip (PRD §6.4 / F-P-1..3)."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from epublate.db import connect, repo

EXPECTED_TABLES = {
    "project",
    "chapter",
    "segment",
    "glossary_entry",
    "glossary_alias",
    "glossary_revision",
    "entity_mention",
    "llm_call",
    "embedding",
    "event",
}


def _existing_tables(engine: Engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).all()
    return {r[0] for r in rows}


def test_connect_creates_database_file(tmp_path: Path) -> None:
    db_path = tmp_path / "demo.epublate"
    assert not db_path.exists()
    engine = connect(db_path)
    try:
        assert db_path.exists()
    finally:
        engine.dispose()


def test_all_prd_tables_exist(project_db: Engine) -> None:
    tables = _existing_tables(project_db)
    missing = EXPECTED_TABLES - tables
    assert not missing, f"missing tables: {sorted(missing)}"


def test_wal_journal_mode(project_db: Engine) -> None:
    with project_db.connect() as conn:
        mode = conn.execute(text("PRAGMA journal_mode")).scalar_one()
    assert str(mode).lower() == "wal"


def test_foreign_keys_pragma(project_db: Engine) -> None:
    with project_db.connect() as conn:
        enabled = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
    assert int(enabled) == 1


def test_project_round_trip(project_db: Engine) -> None:
    created = repo.create_project(
        project_db,
        name="Demo",
        source_lang="en",
        target_lang="pt",
        source_path="/tmp/demo.epub",
    )
    fetched = repo.get_project(project_db, created.id)
    assert fetched is not None
    assert fetched.name == "Demo"
    assert fetched.source_lang == "en"
    assert fetched.target_lang == "pt"
    assert fetched.created_at == created.created_at


def test_event_append_only(project_db: Engine) -> None:
    project = repo.create_project(
        project_db,
        name="Events",
        source_lang="en",
        target_lang="es",
        source_path="/tmp/x.epub",
    )
    repo.append_event(
        project_db, project_id=project.id, kind="project.created", payload={"v": 1}
    )
    repo.append_event(
        project_db, project_id=project.id, kind="batch.started", payload={"chapters": 3}
    )
    events = repo.list_events(project_db, project.id)
    assert [e.kind for e in events] == ["project.created", "batch.started"]
    assert events[0].payload == {"v": 1}
    assert events[1].payload == {"chapters": 3}


def test_get_project_missing_returns_none(project_db: Engine) -> None:
    assert repo.get_project(project_db, "does-not-exist") is None


def test_apply_migrations_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "twice.epublate"
    engine = connect(db_path)
    try:
        from epublate.db import apply_migrations

        apply_migrations(engine)  # second run must be a no-op
        tables = _existing_tables(engine)
        assert tables >= EXPECTED_TABLES
    finally:
        engine.dispose()


def test_migration_failure_raises_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from epublate import db as db_module
    from epublate.errors import MigrationError

    def _broken_upgrade(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("boom")

    db_path = tmp_path / "broken.epublate"
    engine = db_module._engine_for(db_path)
    try:
        monkeypatch.setattr("alembic.command.upgrade", _broken_upgrade)
        with pytest.raises(MigrationError):
            db_module.apply_migrations(engine)
    finally:
        engine.dispose()
