"""Tests for the Settings screen (PRD §4.6 / M6)."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from epublate.app.config import UIConfig
from epublate.app.main import EpublateApp
from epublate.app.screens.dashboard import DashboardScreen
from epublate.app.screens.settings import (
    SettingsScreen,
    StyleEditModal,
    StyleEditResult,
    _first_paragraph,
    _redact_api_key,
)
from epublate.core.project import Project
from epublate.core.style import (
    DEFAULT_STYLE_PROFILE,
    PROFILE_REGISTRY,
    label_for,
    resolve_style_guide,
)
from epublate.db import repo
from epublate.llm.factory import ENV_API_KEY, ENV_BASE_URL, ENV_MODEL
from epublate.llm.mock import MockLLMProvider


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(chapters=[("Solo", "<p>Hello.</p>")])
    return Project.create(
        src, out_dir=tmp_path / "proj", source_lang="en", target_lang="pt"
    )


@pytest.fixture
def llm_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv(ENV_BASE_URL, "https://example.com/v1")
    monkeypatch.setenv(ENV_API_KEY, "sk-fake-1234567890")
    monkeypatch.setenv(ENV_MODEL, "gpt-5-mini")
    yield


def test_redact_api_key_unset() -> None:
    assert _redact_api_key(None) == "(unset)"
    assert _redact_api_key("") == "(unset)"


def test_redact_api_key_short_keys_are_fully_masked() -> None:
    assert _redact_api_key("short") == "***"


def test_redact_api_key_keeps_only_prefix_and_suffix() -> None:
    out = _redact_api_key("sk-fake-1234567890")
    assert out.startswith("sk-f")
    assert out.endswith("90")
    assert "1234" not in out


@pytest.mark.asyncio
async def test_settings_screen_renders_redacted_api_key(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    llm_env: None,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = SettingsScreen(
            project,
            ui_config=UIConfig(theme="textual-dark"),
            default_model="gpt-5-mini",
        )
        config_path = tmp_path / "ui.toml"
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)
            text = str(current.query_one("#settings-llm-body").render())
            assert "sk-f" in text
            assert "1234567890" not in text
            assert "https://example.com/v1" in text
    finally:
        project.close()


@pytest.mark.asyncio
async def test_settings_screen_shows_budget_when_set(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        repo.update_project_budget(
            project.engine, project_id=project.project_id, budget_usd=2.5
        )
        screen = SettingsScreen(project)
        config_path = tmp_path / "ui.toml"
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)
            body = str(current.query_one("#settings-project-body").render())
            assert "$2.5000" in body
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_s_binding_pushes_settings_screen(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = DashboardScreen(project, provider_factory=MockLLMProvider)
        config_path = tmp_path / "ui.toml"
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            assert isinstance(pilot.app.screen, SettingsScreen)
            await pilot.press("q")
            await pilot.pause()
            assert isinstance(pilot.app.screen, DashboardScreen)
    finally:
        project.close()


def test_first_paragraph_flattens_line_breaks_until_blank() -> None:
    """``_first_paragraph`` joins wrapped lines with spaces."""

    out = _first_paragraph("Line one\nstill one.\n\nLine two stays out.")
    assert out == "Line one still one."


def test_first_paragraph_skips_leading_blanks() -> None:
    out = _first_paragraph("\n\n  hello\nworld\n")
    assert out == "hello world"


@pytest.mark.asyncio
async def test_settings_screen_renders_style_block_for_default_profile(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Default-tone projects show the preset name + a prose preview."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = SettingsScreen(project)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)
            body = str(current.query_one("#settings-style-body").render())
            assert label_for(DEFAULT_STYLE_PROFILE) in body
            assert "preview" in body
            assert "(no style guide" not in body
    finally:
        project.close()


@pytest.mark.asyncio
async def test_settings_screen_renders_style_block_for_no_guide(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Projects opted out of the style guide render the empty-state copy."""

    src = tiny_epub_factory(chapters=[("Solo", "<p>Hello.</p>")])
    project = Project.create(
        src,
        out_dir=tmp_path / "no-style",
        source_lang="en",
        target_lang="pt",
        style_profile=None,
        style_guide=None,
    )
    try:
        screen = SettingsScreen(project)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)
            body = str(current.query_one("#settings-style-body").render())
            assert "(no style guide" in body
    finally:
        project.close()


@pytest.mark.asyncio
async def test_settings_screen_e_binding_opens_style_modal(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Pressing ``E`` on the Settings screen surfaces :class:`StyleEditModal`."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = SettingsScreen(project)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(pilot.app.screen, StyleEditModal)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(pilot.app.screen, SettingsScreen)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_style_edit_modal_save_updates_project_and_status(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Saving a new preset persists via Project.update_style + refreshes view."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = SettingsScreen(project)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            modal = pilot.app.screen
            assert isinstance(modal, StyleEditModal)
            from textual.widgets import Select, TextArea

            select = modal.query_one("#style-edit-select", Select)
            select.value = "children_picture"
            await pilot.pause()
            text_area = modal.query_one("#style-edit-text", TextArea)
            assert (
                text_area.text.strip()
                == PROFILE_REGISTRY["children_picture"].prompt_block.strip()
            )
            await pilot.press("ctrl+s")
            await pilot.pause()

            assert isinstance(pilot.app.screen, SettingsScreen)
            current = pilot.app.screen
            assert project.style_profile == "children_picture"
            assert project.style_guide == resolve_style_guide(
                profile_id="children_picture"
            )
            status = str(current.query_one("#settings-status").render())
            assert label_for("children_picture") in status

            row = repo.get_project(project.engine, project.project_id)
            assert row is not None
            assert row.style_profile == "children_picture"

            events = repo.list_events(project.engine, project_id=project.project_id)
            assert any(evt.kind == "project.style_changed" for evt in events)
    finally:
        project.close()


@pytest.mark.asyncio
async def test_style_edit_modal_cancel_leaves_project_unchanged(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Escape + Cancel button must not alter the project's style settings."""

    project = _make_project(tiny_epub_factory, tmp_path)
    original_profile = project.style_profile
    original_guide = project.style_guide
    try:
        screen = SettingsScreen(project)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            assert isinstance(pilot.app.screen, StyleEditModal)
            await pilot.press("escape")
            await pilot.pause()
            assert isinstance(pilot.app.screen, SettingsScreen)
            assert project.style_profile == original_profile
            assert project.style_guide == original_guide
    finally:
        project.close()


@pytest.mark.asyncio
async def test_style_edit_modal_clears_guide_when_none_selected(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Picking 'None' wipes both ``style_profile`` and ``style_guide``."""

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = SettingsScreen(project)
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            modal = pilot.app.screen
            assert isinstance(modal, StyleEditModal)
            from textual.widgets import Select

            modal.query_one("#style-edit-select", Select).value = "__none__"
            await pilot.pause()
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert project.style_profile is None
            assert project.style_guide is None
    finally:
        project.close()


def test_style_edit_result_dataclass_is_frozen() -> None:
    """``StyleEditResult`` must round-trip immutably so handlers can compare."""

    a = StyleEditResult(profile="children_picture", custom_text=None)
    b = StyleEditResult(profile="children_picture", custom_text=None)
    assert a == b
    with pytest.raises(dataclasses.FrozenInstanceError):
        a.profile = "explicit_adult"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Auto tone-sniff toggle (PRD F-STYLE-4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_screen_a_binding_toggles_auto_tone_sniff(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Pressing ``A`` flips :attr:`UIConfig.auto_tone_sniff` and persists it."""

    project = _make_project(tiny_epub_factory, tmp_path)
    config_path = tmp_path / "ui.toml"
    try:
        screen = SettingsScreen(
            project,
            ui_config=UIConfig(auto_tone_sniff=True),
            config_path=config_path,
        )
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)
            ui_body = str(current.query_one("#settings-ui-body").render())
            assert "auto tone-sniff : on" in ui_body

            await pilot.press("a")
            await pilot.pause()

            ui_body_after = str(current.query_one("#settings-ui-body").render())
            assert "auto tone-sniff : off" in ui_body_after

            persisted = UIConfig.load(config_path)
            assert persisted.auto_tone_sniff is False
            status = str(current.query_one("#settings-status").render())
            assert "off" in status

            # Press again to toggle back on.
            await pilot.press("a")
            await pilot.pause()
            assert UIConfig.load(config_path).auto_tone_sniff is True
    finally:
        project.close()


@pytest.mark.asyncio
async def test_settings_screen_announces_env_override(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``EPUBLATE_AUTO_TONE_SNIFF`` pins a value that contradicts the
    persisted bool the panel says so, and the toggle status surfaces the
    env-var-wins case so the curator isn't confused by their key not
    "taking effect"."""

    from epublate.app.config import ENV_AUTO_TONE_SNIFF

    # Force-on env override; persisted bool starts True. After pressing
    # A the persisted bool flips to False, but the env var still pins
    # the *effective* value to on — exactly the case we want to surface.
    monkeypatch.setenv(ENV_AUTO_TONE_SNIFF, "true")

    project = _make_project(tiny_epub_factory, tmp_path)
    config_path = tmp_path / "ui.toml"
    try:
        screen = SettingsScreen(
            project,
            ui_config=UIConfig(auto_tone_sniff=True),
            config_path=config_path,
        )
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)
            body_initial = str(current.query_one("#settings-ui-body").render())
            # Persisted True + env true → effective on, label says "also via".
            assert "auto tone-sniff : on" in body_initial
            assert "also via" in body_initial

            await pilot.press("a")
            await pilot.pause()

            persisted = UIConfig.load(config_path)
            assert persisted.auto_tone_sniff is False
            body_after = str(current.query_one("#settings-ui-body").render())
            # Effective stays on (env wins) but the row should now say
            # "pinned by" because persisted disagrees.
            assert "auto tone-sniff : on" in body_after
            assert "pinned by" in body_after
            status = str(current.query_one("#settings-status").render())
            assert "pins it on" in status
    finally:
        project.close()
