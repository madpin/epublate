"""Snapshot baseline for the Settings screen (PRD §4.6 / M6).

The Settings screen renders environment-driven LLM config; we pin the
relevant ``EPUBLATE_LLM_*`` env vars before taking the screenshot so
the snapshot is deterministic regardless of the contributor's shell.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.app.config import UIConfig
from epublate.app.main import EpublateApp
from epublate.app.screens.settings import SettingsScreen
from epublate.core.project import Project
from epublate.llm.factory import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_HELPER_MODEL,
    ENV_MODEL,
    ENV_ORG,
    ENV_PROVIDER,
)
from tests._snapshot_helpers import mask_settings_paths

TERMINAL_SIZE: tuple[int, int] = (120, 36)


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(chapters=[("Chapter One", "<p>Hello.</p>")])
    return Project.create(
        src,
        out_dir=tmp_path / "snapshot-proj",
        source_lang="en",
        target_lang="pt",
    )


def test_snapshot_settings(
    snap_compare: Callable[..., bool],
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(ENV_PROVIDER, "openai-compat")
    monkeypatch.setenv(ENV_BASE_URL, "https://api.example.com/v1")
    monkeypatch.setenv(ENV_API_KEY, "sk-snapshot-1234567890")
    monkeypatch.setenv(ENV_MODEL, "gpt-5-mini")
    monkeypatch.setenv(ENV_HELPER_MODEL, "gpt-5-mini")
    monkeypatch.delenv(ENV_ORG, raising=False)

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = SettingsScreen(
            project,
            ui_config=UIConfig(theme="textual-dark"),
            default_model="gpt-5-mini",
        )
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        assert snap_compare(
            app,
            terminal_size=TERMINAL_SIZE,
            run_before=mask_settings_paths,
        )
    finally:
        project.close()
