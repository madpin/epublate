"""Unit tests for the recents store (PRD §4.6)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from epublate.app.recents import (
    MAX_RECENTS,
    RecentProject,
    RecentsStore,
    record_project,
)


def test_load_returns_empty_when_file_missing(tmp_path: Path) -> None:
    store = RecentsStore.load(tmp_path / "nope.json")
    assert store.entries == []


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "recents.json"
    store = RecentsStore()
    store.upsert(
        RecentProject(
            project_dir=str(tmp_path / "p1"),
            name="Alpha",
            source_lang="en",
            target_lang="pt",
            last_opened=1000.0,
        )
    )
    store.upsert(
        RecentProject(
            project_dir=str(tmp_path / "p2"),
            name="Beta",
            source_lang="ja",
            target_lang="en",
            last_opened=2000.0,
        )
    )
    store.save(path)

    loaded = RecentsStore.load(path)
    assert [e.name for e in loaded.entries] == ["Beta", "Alpha"]
    assert loaded.entries[0].source_lang == "ja"
    assert loaded.entries[0].target_lang == "en"


def test_upsert_dedupes_on_resolved_path(tmp_path: Path) -> None:
    store = RecentsStore()
    project_dir = tmp_path / "demo"
    project_dir.mkdir()
    store.upsert(
        RecentProject(
            project_dir=str(project_dir),
            name="One",
            source_lang="en",
            target_lang="pt",
            last_opened=100.0,
        )
    )
    store.upsert(
        RecentProject(
            project_dir=str(project_dir / ".."),
            name="Should not match",
            source_lang="en",
            target_lang="pt",
            last_opened=200.0,
        )
    )
    # Different resolved path → 2 entries
    assert len(store.entries) == 2
    # Same resolved path → 1 entry
    store.upsert(
        RecentProject(
            project_dir=str(project_dir),
            name="One Again",
            source_lang="en",
            target_lang="es",
            last_opened=300.0,
        )
    )
    assert len(store.entries) == 2
    assert store.entries[0].name == "One Again"
    assert store.entries[0].target_lang == "es"


def test_upsert_caps_at_max_recents(tmp_path: Path) -> None:
    store = RecentsStore()
    for i in range(MAX_RECENTS + 5):
        store.upsert(
            RecentProject(
                project_dir=str(tmp_path / f"p{i}"),
                name=f"P{i}",
                source_lang="en",
                target_lang="pt",
                last_opened=float(i),
            )
        )
    assert len(store.entries) == MAX_RECENTS


def test_remove_returns_whether_entry_existed(tmp_path: Path) -> None:
    store = RecentsStore()
    store.upsert(
        RecentProject(
            project_dir=str(tmp_path / "x"),
            name="X",
            source_lang="en",
            target_lang="pt",
        )
    )
    assert store.remove(tmp_path / "x") is True
    assert store.remove(tmp_path / "x") is False


def test_prune_missing_only_drops_dead_entries(tmp_path: Path) -> None:
    alive = tmp_path / "alive"
    alive.mkdir()
    store = RecentsStore()
    store.upsert(
        RecentProject(
            project_dir=str(alive),
            name="alive",
            source_lang="en",
            target_lang="pt",
            last_opened=100.0,
        )
    )
    store.upsert(
        RecentProject(
            project_dir=str(tmp_path / "ghost"),
            name="ghost",
            source_lang="en",
            target_lang="pt",
            last_opened=200.0,
        )
    )
    pruned = store.prune_missing()
    assert [e.name for e in pruned] == ["ghost"]
    assert [e.name for e in store.entries] == ["alive"]


def test_load_ignores_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "recents.json"
    path.write_text("{not valid json", encoding="utf-8")
    assert RecentsStore.load(path).entries == []


def test_record_project_writes_to_file(tmp_path: Path) -> None:
    path = tmp_path / "recents.json"
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    record_project(
        project_dir=project_dir,
        name="Proj",
        source_lang="en",
        target_lang="pt",
        path=path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["entries"][0]["name"] == "Proj"
    assert payload["entries"][0]["target_lang"] == "pt"
    assert payload["schema_version"] == 1
    # Calling again bumps last_opened and dedupes.
    earlier = payload["entries"][0]["last_opened"]
    time.sleep(0.01)
    record_project(
        project_dir=project_dir,
        name="Proj",
        source_lang="en",
        target_lang="pt",
        path=path,
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["entries"]) == 1
    assert payload["entries"][0]["last_opened"] >= earlier
