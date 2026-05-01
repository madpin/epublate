"""Project-row style_profile / style_guide persistence tests (F-STYLE-1, F-STYLE-2)."""

from __future__ import annotations

from sqlalchemy.engine import Engine

from epublate.db import repo


def test_create_project_persists_style_columns(project_db: Engine) -> None:
    row = repo.create_project(
        project_db,
        name="t",
        source_lang="en",
        target_lang="pt",
        source_path="/tmp/x.epub",
        style_guide="my prose",
        style_profile="young_adult",
    )
    fetched = repo.get_project(project_db, row.id)
    assert fetched is not None
    assert fetched.style_profile == "young_adult"
    assert fetched.style_guide == "my prose"


def test_update_project_style_replaces_both_fields(project_db: Engine) -> None:
    row = repo.create_project(
        project_db,
        name="t",
        source_lang="en",
        target_lang="pt",
        source_path="/tmp/x.epub",
        style_profile="literary_fiction",
        style_guide="initial",
    )
    refreshed = repo.update_project_style(
        project_db,
        project_id=row.id,
        style_profile="children_picture",
        style_guide="custom kid voice",
    )
    assert refreshed.style_profile == "children_picture"
    assert refreshed.style_guide == "custom kid voice"


def test_update_project_style_records_event(project_db: Engine) -> None:
    row = repo.create_project(
        project_db,
        name="t",
        source_lang="en",
        target_lang="pt",
        source_path="/tmp/x.epub",
        style_profile="literary_fiction",
        style_guide="A",
    )
    repo.update_project_style(
        project_db,
        project_id=row.id,
        style_profile="explicit_adult",
        style_guide="B",
    )
    events = [
        e
        for e in repo.list_events(project_db, row.id)
        if e.kind == "project.style_changed"
    ]
    assert len(events) == 1
    payload = events[0].payload
    assert payload["prev_profile"] == "literary_fiction"
    assert payload["new_profile"] == "explicit_adult"
    assert payload["prev_guide_set"] is True
    assert payload["new_guide_set"] is True


def test_update_project_style_clears_to_none(project_db: Engine) -> None:
    row = repo.create_project(
        project_db,
        name="t",
        source_lang="en",
        target_lang="pt",
        source_path="/tmp/x.epub",
        style_profile="literary_fiction",
        style_guide="something",
    )
    cleared = repo.update_project_style(
        project_db,
        project_id=row.id,
        style_profile=None,
        style_guide=None,
    )
    assert cleared.style_profile is None
    assert cleared.style_guide is None
