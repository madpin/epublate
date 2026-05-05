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


# ---------------------------------------------------------------------------
# Editable panels (PRD §4.6 / M6 — settings overhaul)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_settings_project_panel_saves_name_and_budget(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """The Project tab's Save button persists name + budget via the repo."""

    from textual.widgets import Button, Input

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = SettingsScreen(project, config_path=tmp_path / "ui.toml")
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)
            current.query_one("#settings-project-name", Input).value = "Renamed Book"
            current.query_one("#settings-project-budget", Input).value = "3.25"
            current.query_one("#settings-project-save", Button).press()
            await pilot.pause()

            row = repo.get_project(project.engine, project.project_id)
            assert row is not None
            assert row.name == "Renamed Book"
            assert row.budget_usd == pytest.approx(3.25)
            assert project.name == "Renamed Book"

            body = str(current.query_one("#settings-project-body").render())
            assert "Renamed Book" in body
            assert "$3.2500" in body
    finally:
        project.close()


@pytest.mark.asyncio
async def test_settings_project_panel_blank_budget_clears(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Clearing the budget input must wipe the persisted cap."""

    from textual.widgets import Button, Input

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        repo.update_project_budget(
            project.engine, project_id=project.project_id, budget_usd=5.0
        )
        screen = SettingsScreen(project, config_path=tmp_path / "ui.toml")
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)
            current.query_one("#settings-project-budget", Input).value = ""
            current.query_one("#settings-project-save", Button).press()
            await pilot.pause()
            row = repo.get_project(project.engine, project.project_id)
            assert row is not None
            assert row.budget_usd is None
    finally:
        project.close()


@pytest.mark.asyncio
async def test_settings_llm_panel_saves_overrides(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    llm_env: None,
) -> None:
    """The LLM tab persists per-project overrides as a JSON blob."""

    from textual.widgets import Button, Input

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = SettingsScreen(
            project, default_model="gpt-5-mini", config_path=tmp_path / "ui.toml"
        )
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)
            current.query_one(
                "#settings-llm-base-url", Input
            ).value = "https://router.example/v1"
            current.query_one(
                "#settings-llm-translator-model", Input
            ).value = "gpt-5-pro"
            current.query_one("#settings-llm-helper-model", Input).value = "gpt-5-nano"
            current.query_one("#settings-llm-save", Button).press()
            await pilot.pause()

            persisted = repo.get_llm_overrides(project.engine, project.project_id)
            assert persisted == {
                "base_url": "https://router.example/v1",
                "translator_model": "gpt-5-pro",
                "helper_model": "gpt-5-nano",
            }

            body = str(current.query_one("#settings-llm-body").render())
            assert "gpt-5-pro" in body
            assert "router.example" in body
            assert "(override)" in body

            current.query_one("#settings-llm-clear", Button).press()
            await pilot.pause()
            assert repo.get_llm_overrides(project.engine, project.project_id) == {}
            body_cleared = str(current.query_one("#settings-llm-body").render())
            assert "overrides    : none" in body_cleared
    finally:
        project.close()


@pytest.mark.asyncio
async def test_settings_intake_panel_saves_to_ui_config(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Intake defaults persist to the UIConfig snapshot + the TOML file."""

    from textual.widgets import Button, Input

    project = _make_project(tiny_epub_factory, tmp_path)
    config_path = tmp_path / "ui.toml"
    ui_config = UIConfig()
    try:
        screen = SettingsScreen(project, ui_config=ui_config, config_path=config_path)
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)
            current.query_one(
                "#settings-intake-helper-model", Input
            ).value = "gpt-5-nano"
            current.query_one("#settings-intake-max-segments", Input).value = "75"
            current.query_one("#settings-intake-run-after", Input).value = "y"
            current.query_one("#settings-intake-save", Button).press()
            await pilot.pause()

            assert ui_config.intake_helper_model == "gpt-5-nano"
            assert ui_config.intake_max_segments == 75
            assert ui_config.intake_run_after_new is True

            persisted = UIConfig.load(config_path)
            assert persisted.intake_helper_model == "gpt-5-nano"
            assert persisted.intake_max_segments == 75
            assert persisted.intake_run_after_new is True

            body = str(current.query_one("#settings-intake-body").render())
            assert "gpt-5-nano" in body
            assert "75" in body
            assert "on" in body
    finally:
        project.close()


@pytest.mark.asyncio
async def test_settings_concurrency_panel_validates_and_saves(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Concurrency tab persists ints; an invalid value is reported, not raised."""

    from textual.widgets import Button, Input

    project = _make_project(tiny_epub_factory, tmp_path)
    config_path = tmp_path / "ui.toml"
    ui_config = UIConfig()
    try:
        screen = SettingsScreen(project, ui_config=ui_config, config_path=config_path)
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)
            current.query_one("#settings-concurrency-batch", Input).value = "0"
            current.query_one("#settings-concurrency-save", Button).press()
            await pilot.pause()
            status = str(current.query_one("#settings-status").render())
            assert "Concurrency must be at least 1" in status
            assert ui_config.batch_concurrency == 1

            current.query_one("#settings-concurrency-batch", Input).value = "4"
            current.query_one("#settings-concurrency-retries", Input).value = "3"
            current.query_one("#settings-concurrency-save", Button).press()
            await pilot.pause()

            assert ui_config.batch_concurrency == 4
            assert ui_config.batch_retries == 3
            persisted = UIConfig.load(config_path)
            assert persisted.batch_concurrency == 4
            assert persisted.batch_retries == 3
    finally:
        project.close()


@pytest.mark.asyncio
async def test_settings_ui_panel_saves_theme_dropdown(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Theme dropdown saves to the live ``UIConfig`` and persists to disk."""

    from textual.widgets import Button, Select

    from epublate.app.themes import EPUBLATE_THEME_ORDER

    project = _make_project(tiny_epub_factory, tmp_path)
    config_path = tmp_path / "ui.toml"
    ui_config = UIConfig(theme=EPUBLATE_THEME_ORDER[0])
    try:
        screen = SettingsScreen(project, ui_config=ui_config, config_path=config_path)
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)
            target_theme = EPUBLATE_THEME_ORDER[-1]
            select = current.query_one("#settings-ui-theme", Select)
            select.value = target_theme
            await pilot.pause()
            current.query_one("#settings-ui-save", Button).press()
            await pilot.pause()

            assert pilot.app.theme == target_theme
            assert ui_config.theme == target_theme
            persisted = UIConfig.load(config_path)
            assert persisted.theme == target_theme
    finally:
        project.close()


@pytest.mark.asyncio
async def test_dashboard_propagates_ui_config_to_settings(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Edits made on the Settings screen survive a Dashboard round-trip.

    Regression test for the original drift bug: Dashboard pushed
    Settings without a ``UIConfig`` snapshot, so any persisted edit
    was silently re-loaded from disk and ignored on Dashboard refresh.
    Now both screens share the same in-memory snapshot.
    """

    from textual.widgets import Button, Input

    from epublate.app.themes import EPUBLATE_THEME_ORDER

    project = _make_project(tiny_epub_factory, tmp_path)
    config_path = tmp_path / "ui.toml"
    ui_config = UIConfig(theme=EPUBLATE_THEME_ORDER[0], batch_concurrency=1)
    try:
        screen = DashboardScreen(
            project,
            provider_factory=MockLLMProvider,
            ui_config=ui_config,
            config_path=config_path,
        )
        app = EpublateApp(initial_screen=screen, config_path=config_path)
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            settings = pilot.app.screen
            assert isinstance(settings, SettingsScreen)
            assert settings.ui_config is ui_config

            settings.query_one("#settings-concurrency-batch", Input).value = "5"
            settings.query_one("#settings-concurrency-save", Button).press()
            await pilot.pause()
            assert ui_config.batch_concurrency == 5

            await pilot.press("q")
            await pilot.pause()
            dashboard = pilot.app.screen
            assert isinstance(dashboard, DashboardScreen)
            # The Dashboard's snapshot is the same object Settings just
            # mutated, so its BatchModal would default to 5 concurrency.
            assert dashboard._ui_config is ui_config  # type: ignore[attr-defined]
            assert dashboard._ui_config.batch_concurrency == 5  # type: ignore[attr-defined]
    finally:
        project.close()


@pytest.mark.asyncio
async def test_settings_project_panel_saves_context_defaults(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """The Project tab persists the per-project preceding-segment
    context defaults so the Reader and Dashboard pre-fills inherit
    them on the next translate (PRD §4.6 / §5)."""

    from textual.widgets import Button, Input

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        screen = SettingsScreen(project, config_path=tmp_path / "ui.toml")
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)

            current.query_one("#settings-project-context-segments", Input).value = "4"
            current.query_one("#settings-project-context-chars", Input).value = "750"
            current.query_one("#settings-project-save", Button).press()
            await pilot.pause()

            row = repo.get_project(project.engine, project.project_id)
            assert row is not None
            assert row.context_max_segments == 4
            assert row.context_max_chars == 750

            body = str(current.query_one("#settings-project-body").render())
            assert "4" in body and "750" in body
    finally:
        project.close()


@pytest.mark.asyncio
async def test_settings_project_panel_blank_context_clears_defaults(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
) -> None:
    """Clearing both context inputs zeroes the defaults (off / no cap)."""

    from textual.widgets import Button, Input

    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        repo.update_project_context_defaults(
            project.engine,
            project_id=project.project_id,
            max_segments=4,
            max_chars=600,
        )

        screen = SettingsScreen(project, config_path=tmp_path / "ui.toml")
        app = EpublateApp(initial_screen=screen, config_path=tmp_path / "ui.toml")
        async with app.run_test() as pilot:
            await pilot.pause()
            current = pilot.app.screen
            assert isinstance(current, SettingsScreen)
            current.query_one("#settings-project-context-segments", Input).value = ""
            current.query_one("#settings-project-context-chars", Input).value = ""
            current.query_one("#settings-project-save", Button).press()
            await pilot.pause()
            row = repo.get_project(project.engine, project.project_id)
            assert row is not None
            assert row.context_max_segments == 0
            assert row.context_max_chars == 0
    finally:
        project.close()
