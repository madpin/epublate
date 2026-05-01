"""``Project.create`` style wiring tests (PRD F-STYLE-1)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from epublate.core.project import Project
from epublate.core.style import DEFAULT_STYLE_PROFILE, PROFILE_REGISTRY


def test_create_defaults_to_literary_fiction(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    project = Project.create(
        src, out_dir=tmp_path / "p", source_lang="en", target_lang="pt"
    )
    try:
        assert project.style_profile == DEFAULT_STYLE_PROFILE
        assert project.style_guide is not None
        assert (
            project.style_guide == PROFILE_REGISTRY[DEFAULT_STYLE_PROFILE].prompt_block
        )
    finally:
        project.close()


def test_create_records_profile_in_project_event(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    project = Project.create(
        src,
        out_dir=tmp_path / "p",
        source_lang="en",
        target_lang="pt",
        style_profile="children_picture",
    )
    try:
        from epublate.db import repo

        events = [
            e
            for e in repo.list_events(project.engine, project.project_id)
            if e.kind == "project.created"
        ]
        assert len(events) == 1
        payload = events[0].payload
        assert payload["style_profile"] == "children_picture"
        assert payload["style_guide_set"] is True
    finally:
        project.close()


def test_create_with_explicit_profile_picks_that_preset(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    project = Project.create(
        src,
        out_dir=tmp_path / "p",
        source_lang="en",
        target_lang="pt",
        style_profile="explicit_adult",
    )
    try:
        assert project.style_profile == "explicit_adult"
        assert project.style_guide is not None
        assert "explicit" in project.style_guide.lower()
    finally:
        project.close()


def test_create_with_custom_text_overrides_preset(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    project = Project.create(
        src,
        out_dir=tmp_path / "p",
        source_lang="en",
        target_lang="pt",
        style_profile="literary_fiction",
        style_guide="explicit prose override",
    )
    try:
        # The preset slug is preserved (so the UI can show "Literary fiction
        # (custom)"), but the resolved prompt block is the user's text.
        assert project.style_profile == "literary_fiction"
        assert project.style_guide == "explicit prose override"
    finally:
        project.close()


def test_create_with_no_style_opts_out(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    project = Project.create(
        src,
        out_dir=tmp_path / "p",
        source_lang="en",
        target_lang="pt",
        style_profile=None,
    )
    try:
        assert project.style_profile is None
        assert project.style_guide is None
    finally:
        project.close()


def test_update_style_changes_profile_and_guide(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    src = tiny_epub_factory()
    project = Project.create(
        src,
        out_dir=tmp_path / "p",
        source_lang="en",
        target_lang="pt",
        style_profile="literary_fiction",
    )
    try:
        refreshed = project.update_style(style_profile="young_adult")
        assert refreshed.style_profile == "young_adult"
        assert project.style_profile == "young_adult"
        assert project.style_guide is not None
        assert project.style_guide == PROFILE_REGISTRY["young_adult"].prompt_block

        refreshed = project.update_style(
            style_profile="literary_fiction",
            custom_text="my own paragraph for voice",
        )
        assert refreshed.style_guide == "my own paragraph for voice"
        assert project.style_guide == "my own paragraph for voice"

        refreshed = project.update_style(style_profile=None)
        assert refreshed.style_profile is None
        assert project.style_profile is None
        assert project.style_guide is None
    finally:
        project.close()
