"""Tests for ``attached_lore`` CRUD helpers (PRD §4.3 / F-LB-10 phase 3)."""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine

from epublate.db import repo
from epublate.db.schema import AttachedLoreMode


def _ensure_project(engine: Engine, project_id: str = "proj-1") -> str:
    repo.create_project(
        engine,
        name="demo",
        source_lang="en",
        target_lang="pt",
        source_path="/dev/null",
        project_id=project_id,
    )
    return project_id


def test_attach_then_list_appends_with_increasing_priority(
    project_db: Engine,
) -> None:
    pid = _ensure_project(project_db)
    first = repo.attach_lore_book(
        project_db,
        project_id=pid,
        lore_path="/tmp/lore-a.epublate-lore",
    )
    second = repo.attach_lore_book(
        project_db,
        project_id=pid,
        lore_path="/tmp/lore-b.epublate-lore",
    )
    third = repo.attach_lore_book(
        project_db,
        project_id=pid,
        lore_path="/tmp/lore-c.epublate-lore",
    )
    assert (first.priority, second.priority, third.priority) == (0, 1, 2)

    rows = repo.list_attached_lore(project_db, project_id=pid)
    assert [r.lore_path for r in rows] == [
        "/tmp/lore-a.epublate-lore",
        "/tmp/lore-b.epublate-lore",
        "/tmp/lore-c.epublate-lore",
    ]


def test_attach_with_explicit_priority_inserts_in_order(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    repo.attach_lore_book(
        project_db,
        project_id=pid,
        lore_path="/tmp/lore-mid.epublate-lore",
        priority=5,
    )
    repo.attach_lore_book(
        project_db,
        project_id=pid,
        lore_path="/tmp/lore-top.epublate-lore",
        priority=0,
    )
    repo.attach_lore_book(
        project_db,
        project_id=pid,
        lore_path="/tmp/lore-bot.epublate-lore",
        priority=10,
    )
    rows = repo.list_attached_lore(project_db, project_id=pid)
    assert [r.lore_path for r in rows] == [
        "/tmp/lore-top.epublate-lore",
        "/tmp/lore-mid.epublate-lore",
        "/tmp/lore-bot.epublate-lore",
    ]


def test_attach_is_idempotent_and_updates_mode(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    first = repo.attach_lore_book(
        project_db,
        project_id=pid,
        lore_path="/tmp/lore.epublate-lore",
    )
    assert first.mode == AttachedLoreMode.READ_ONLY

    second = repo.attach_lore_book(
        project_db,
        project_id=pid,
        lore_path="/tmp/lore.epublate-lore",
        mode=AttachedLoreMode.WRITABLE,
        priority=5,
    )
    assert second.id == first.id
    assert second.mode == AttachedLoreMode.WRITABLE
    assert second.priority == 5

    rows = repo.list_attached_lore(project_db, project_id=pid)
    assert len(rows) == 1


def test_detach_removes_row(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    repo.attach_lore_book(
        project_db,
        project_id=pid,
        lore_path="/tmp/lore.epublate-lore",
    )
    assert repo.detach_lore_book(
        project_db, project_id=pid, lore_path="/tmp/lore.epublate-lore"
    )
    assert not repo.list_attached_lore(project_db, project_id=pid)


def test_detach_returns_false_for_unknown_path(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    assert not repo.detach_lore_book(
        project_db, project_id=pid, lore_path="/tmp/missing.epublate-lore"
    )


def test_update_attached_lore_patches_mode_and_priority(
    project_db: Engine,
) -> None:
    pid = _ensure_project(project_db)
    repo.attach_lore_book(
        project_db,
        project_id=pid,
        lore_path="/tmp/lore.epublate-lore",
    )
    updated = repo.update_attached_lore(
        project_db,
        project_id=pid,
        lore_path="/tmp/lore.epublate-lore",
        mode=AttachedLoreMode.WRITABLE,
        priority=3,
    )
    assert updated is not None
    assert updated.mode == AttachedLoreMode.WRITABLE
    assert updated.priority == 3


def test_update_attached_lore_returns_none_for_unknown(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    result = repo.update_attached_lore(
        project_db,
        project_id=pid,
        lore_path="/tmp/missing.epublate-lore",
        mode=AttachedLoreMode.WRITABLE,
    )
    assert result is None


def test_attach_rejects_unknown_mode(project_db: Engine) -> None:
    pid = _ensure_project(project_db)
    with pytest.raises(ValueError):
        repo.attach_lore_book(
            project_db,
            project_id=pid,
            lore_path="/tmp/lore.epublate-lore",
            mode="exclusive",
        )
