"""Tests for glossary JSON I/O + auto-proposer (PRD F-LB-8 / M3)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine

from epublate.db import repo
from epublate.errors import ConfigurationError
from epublate.glossary import io as glossary_io


def _project(engine: Engine) -> str:
    repo.create_project(
        engine,
        name="demo",
        source_lang="en",
        target_lang="pt",
        source_path="/dev/null",
        project_id="proj-1",
    )
    return "proj-1"


def test_export_round_trip(project_db: Engine, tmp_path: Path) -> None:
    pid = _project(project_db)
    repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Élise",
        target_term="Elisa",
        type="character",
        status="locked",
        source_aliases=["Lise"],
        target_aliases=["Eli"],
        notes="protagonist",
    )
    payload = glossary_io.export_json(project_db, pid)
    assert payload["version"] == glossary_io.GLOSSARY_FORMAT_VERSION
    assert len(payload["entries"]) == 1
    out = tmp_path / "g.json"
    glossary_io.write_export(out, payload)
    re_read = json.loads(out.read_text(encoding="utf-8"))
    assert re_read == payload


def test_import_creates_new_entries(project_db: Engine) -> None:
    pid = _project(project_db)
    payload = {
        "version": 1,
        "entries": [
            {
                "source_term": "Élise",
                "target_term": "Elisa",
                "type": "character",
                "status": "locked",
                "source_aliases": ["Lise"],
                "target_aliases": ["Eli"],
            }
        ],
    }
    summary = glossary_io.import_json(project_db, project_id=pid, payload=payload)
    assert summary.created == 1
    assert summary.skipped == 0
    fetched = repo.list_glossary_entries(project_db, pid)
    assert len(fetched) == 1
    assert fetched[0].entry.status == "locked"
    assert fetched[0].source_aliases == ["Lise"]


def test_import_skip_strategy_preserves_existing(project_db: Engine) -> None:
    pid = _project(project_db)
    repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Élise",
        target_term="Elisa",
        type="character",
        status="locked",
    )
    payload = {
        "version": 1,
        "entries": [
            {
                "source_term": "Élise",
                "target_term": "DIFFERENT",
                "type": "character",
                "status": "proposed",
            }
        ],
    }
    summary = glossary_io.import_json(
        project_db, project_id=pid, payload=payload, conflict="skip"
    )
    assert summary.created == 0
    assert summary.skipped == 1
    refreshed = repo.list_glossary_entries(project_db, pid)
    assert refreshed[0].target_term == "Elisa"
    assert refreshed[0].status == "locked"


def test_import_overwrite_records_revision(project_db: Engine) -> None:
    pid = _project(project_db)
    entry = repo.create_glossary_entry(
        project_db,
        project_id=pid,
        source_term="Élise",
        target_term="Elisa",
        type="character",
        status="confirmed",
    )
    payload = {
        "version": 1,
        "entries": [
            {
                "source_term": "Élise",
                "target_term": "Elise",
                "type": "character",
                "status": "locked",
                "target_aliases": ["Eli"],
            }
        ],
    }
    summary = glossary_io.import_json(
        project_db, project_id=pid, payload=payload, conflict="overwrite"
    )
    assert summary.updated == 1
    refreshed = repo.list_glossary_entries(project_db, pid)
    assert refreshed[0].target_term == "Elise"
    assert refreshed[0].status == "locked"
    assert refreshed[0].target_aliases == ["Eli"]
    revisions = repo.list_glossary_revisions(project_db, entry.id)
    assert len(revisions) == 1
    assert revisions[0].new_target_term == "Elise"


def test_import_rejects_bad_version(project_db: Engine) -> None:
    pid = _project(project_db)
    with pytest.raises(ConfigurationError):
        glossary_io.import_json(
            project_db, project_id=pid, payload={"version": 99, "entries": []}
        )


def test_import_rejects_bad_status(project_db: Engine) -> None:
    pid = _project(project_db)
    with pytest.raises(ConfigurationError):
        glossary_io.import_json(
            project_db,
            project_id=pid,
            payload={
                "version": 1,
                "entries": [
                    {
                        "source_term": "X",
                        "target_term": "Y",
                        "status": "bogus",
                    }
                ],
            },
        )


def test_starter_import_from_disk(tmp_path: Path, project_db: Engine) -> None:
    pid = _project(project_db)
    payload = {
        "version": 1,
        "entries": [
            {"source_term": "A", "target_term": "a"},
            {"source_term": "B", "target_term": "b", "status": "confirmed"},
        ],
    }
    p = tmp_path / "starter.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    summary = glossary_io.import_starter(project_db, project_id=pid, path=p)
    assert summary.created == 2
    assert summary.skipped == 0


def test_upsert_proposed_dedups(project_db: Engine) -> None:
    pid = _project(project_db)
    eid1, created1 = glossary_io.upsert_proposed(
        project_db,
        project_id=pid,
        source_term="Élise",
        type="character",
    )
    eid2, created2 = glossary_io.upsert_proposed(
        project_db,
        project_id=pid,
        source_term="Élise",
        type="character",
    )
    assert created1 is True
    assert created2 is False
    assert eid1 == eid2


def test_read_payload_validates(tmp_path: Path) -> None:
    p = tmp_path / "garbage.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        glossary_io.read_payload(p)
