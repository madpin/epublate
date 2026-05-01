"""Settings screen — LLM config + UI prefs + style guide (PRD §4.6 / M6).

Most of the screen is read-only: the user-editable settings already live
elsewhere (the project's budget on the Dashboard, LLM config in
environment variables, theme via the ``T`` keystroke). The screen's
job is to make the *current* values discoverable in one place — so the
curator can answer "which model is wired up?" and "is my API key
actually visible?" without leaving the TUI.

The one exception is the **Style guide** panel (PRD F-STYLE-2): the
curator picks a tone preset at New Project time, and the Settings
screen lets them swap presets or edit the prompt prose later via
``E`` → :class:`StyleEditModal`. Changing the style invalidates the
translator cache automatically (the system-prompt hash is part of every
cache key per PRD F-LLM-6) so the next batch picks up the new voice
without any extra plumbing.

The screen also owns the **auto tone-sniff** toggle (PRD F-STYLE-4):
``A`` flips :attr:`UIConfig.auto_tone_sniff`, persists the new value,
and the change applies the next time the curator opens the New Project
modal. ``EPUBLATE_AUTO_TONE_SNIFF`` overrides the persisted value at
runtime; the panel labels it accordingly so the curator knows when an
env var is winning.

Inputs:

* ``project`` — the active :class:`epublate.core.project.Project`. We
  read the budget cap, language pair, and style guide from here.
* ``ui_config`` — the persisted :class:`epublate.app.config.UIConfig`
  whose ``theme`` key tracks what the cycler last saved.
* ``default_model`` — the translator model the Dashboard would dispatch
  on a batch run.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Label, Select, Static, TextArea

from epublate.app.config import (
    ENV_AUTO_TONE_SNIFF,
    UIConfig,
    default_config_path,
    resolve_auto_tone_sniff,
)
from epublate.app.themes import EPUBLATE_THEME_ORDER
from epublate.core.project import Project
from epublate.core.style import (
    DEFAULT_STYLE_PROFILE,
    PROFILE_REGISTRY,
    label_for,
    list_profiles,
)
from epublate.llm.factory import (
    ENV_API_KEY,
    ENV_BASE_URL,
    ENV_HELPER_MODEL,
    ENV_MODEL,
    ENV_ORG,
    ENV_PROVIDER,
)

_logger = logging.getLogger(__name__)

_NONE_PROFILE_VALUE = "__none__"
"""See :data:`epublate.app.screens.new_project._NONE_PROFILE_VALUE`."""


def _first_paragraph(text: str) -> str:
    """Return the first paragraph of ``text`` flattened onto one line.

    The shipped tone presets use natural line breaks inside paragraphs
    so the prompt reads cleanly to the LLM; for the Settings preview
    we want a single readable line, so we collapse line breaks to
    spaces until we hit a blank line.
    """

    parts: list[str] = []
    for raw in text.strip().splitlines():
        line = raw.strip()
        if not line:
            if parts:
                break
            continue
        parts.append(line)
    return " ".join(parts)


@dataclass(slots=True, frozen=True)
class StyleEditResult:
    """Form result emitted by :class:`StyleEditModal` on save.

    ``profile`` is the chosen preset slug (or ``None`` for "no style").
    ``custom_text`` is the verbatim prose the curator authored when it
    differs from the preset; ``None`` means "use the preset's text".
    """

    profile: str | None
    custom_text: str | None


class StyleEditModal(ModalScreen[StyleEditResult | None]):
    """Edit the active project's tone preset + prompt block (PRD F-STYLE-2).

    Mirrors the New Project modal's tone field with two extras: the form
    is pre-populated from the project's *current* values, and the
    curator can return ``StyleEditResult(None, None)`` to clear the
    style guide entirely. Save persists via :meth:`Project.update_style`,
    which records a ``project.style_changed`` event for the audit log.
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+s", "submit", "Save", show=True),
        Binding("escape", "cancel", "Cancel", show=True),
    ]

    DEFAULT_CSS = """
    StyleEditModal {
        align: center middle;
    }
    StyleEditModal #style-edit-box {
        width: 90%;
        max-width: 110;
        height: auto;
        max-height: 90%;
        border: round $accent;
        padding: 1 2;
        background: $panel;
    }
    StyleEditModal #style-edit-title {
        text-style: bold;
        color: $primary;
        padding: 0;
    }
    StyleEditModal #style-edit-subtitle {
        color: $text-muted;
        padding: 0 0 1 0;
        height: 1;
    }
    StyleEditModal .row {
        height: auto;
        padding: 0 0 1 0;
    }
    StyleEditModal .row Label {
        width: 16;
    }
    StyleEditModal Select {
        background: $surface;
    }
    StyleEditModal TextArea {
        height: 10;
        background: $surface;
        border: tall $primary 30%;
    }
    StyleEditModal TextArea:focus {
        border: tall $accent;
    }
    StyleEditModal #style-edit-hint {
        color: $text-muted;
        padding: 0 0 1 0;
        height: auto;
    }
    StyleEditModal #style-edit-buttons {
        height: 3;
        padding: 1 0 0 0;
        align-horizontal: right;
    }
    StyleEditModal #style-edit-buttons Button {
        margin: 0 0 0 1;
    }
    """

    def __init__(
        self,
        *,
        current_profile: str | None,
        current_text: str | None,
    ) -> None:
        super().__init__()
        self._current_profile = current_profile
        self._current_text = current_text or ""

    def compose(self) -> ComposeResult:
        options: list[tuple[str, str]] = [(p.name, p.id) for p in list_profiles()]
        options.append(("None — no style guide", _NONE_PROFILE_VALUE))

        initial_value: str = (
            self._current_profile
            if self._current_profile in PROFILE_REGISTRY
            else _NONE_PROFILE_VALUE
            if self._current_profile is None and not self._current_text
            else DEFAULT_STYLE_PROFILE
        )

        # When the row carries text but no recognized profile slug, fall
        # back to the default profile so the Select renders deterministically;
        # the TextArea still shows the curator's prose verbatim.
        with Vertical(id="style-edit-box"):
            yield Label("Edit style guide", id="style-edit-title")
            yield Label(
                "Pick a tone preset or author your own paragraph. "
                "Changes invalidate the translator cache.",
                id="style-edit-subtitle",
            )
            with Horizontal(classes="row"):
                yield Label("Tone preset:")
                yield Select(
                    options=options,
                    value=initial_value,
                    allow_blank=False,
                    id="style-edit-select",
                )
            yield TextArea(
                text=self._current_text
                or PROFILE_REGISTRY[DEFAULT_STYLE_PROFILE].prompt_block,
                id="style-edit-text",
            )
            yield Static(
                "[dim]The LLM sees this verbatim under [/dim][b]Style guide:[/b]"
                "[dim] in the system prompt. Edit freely.[/dim]",
                id="style-edit-hint",
                markup=True,
            )
            with Horizontal(id="style-edit-buttons"):
                yield Button("Cancel", id="style-edit-cancel", variant="default")
                yield Button("Save", id="style-edit-save", variant="primary")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#style-edit-text", TextArea).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "style-edit-save":
            self.action_submit()
        elif bid == "style-edit-cancel":
            self.action_cancel()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "style-edit-select":
            return
        text_area = self.query_one("#style-edit-text", TextArea)
        chosen = event.value
        if chosen == _NONE_PROFILE_VALUE:
            text_area.text = ""
            text_area.disabled = True
            return
        text_area.disabled = False
        if isinstance(chosen, str) and chosen in PROFILE_REGISTRY:
            text_area.text = PROFILE_REGISTRY[chosen].prompt_block

    def action_submit(self) -> None:
        select = self.query_one("#style-edit-select", Select)
        text_area = self.query_one("#style-edit-text", TextArea)
        raw = select.value
        if raw == _NONE_PROFILE_VALUE or raw is None:
            self.dismiss(StyleEditResult(profile=None, custom_text=None))
            return
        if not isinstance(raw, str) or raw not in PROFILE_REGISTRY:
            self.app.bell()
            return
        profile = raw
        edited = text_area.text
        preset_block = PROFILE_REGISTRY[profile].prompt_block
        custom = None if edited.strip() == preset_block.strip() else edited
        self.dismiss(StyleEditResult(profile=profile, custom_text=custom))

    def action_cancel(self) -> None:
        self.dismiss(None)


def _redact_api_key(raw: str | None) -> str:
    """Show only the leading prefix of an API key (PRD NFR-3 / privacy)."""

    if not raw:
        return "(unset)"
    if len(raw) <= 8:
        return "***"
    return f"{raw[:4]}…{raw[-2:]}"


class SettingsScreen(Screen[None]):
    """Configuration overview + tone editor (PRD §4.6 / M6 / F-STYLE-2)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("e", "edit_style", "Edit style", show=True),
        Binding("a", "toggle_auto_tone_sniff", "Auto-tone", show=True),
        Binding("r", "refresh", "Refresh", show=True),
        Binding("q", "app.pop_screen", "Back", show=True),
        # ``escape`` mirrors ``q`` so curators can back out with the
        # universal "cancel" key; hidden from the footer to avoid
        # cluttering the binding bar with a duplicate label.
        Binding("escape", "app.pop_screen", "Back", show=False),
    ]

    DEFAULT_CSS = """
    SettingsScreen #settings-body {
        height: 1fr;
        padding: 1 2;
    }
    SettingsScreen .panel {
        border: round $primary;
        padding: 1 2;
        margin: 0 0 1 0;
        height: auto;
    }
    SettingsScreen .panel-title {
        text-style: bold;
        padding: 0 0 1 0;
    }
    SettingsScreen #settings-status {
        dock: bottom;
        height: 1;
        padding: 0 1;
        background: $boost;
    }
    """

    def __init__(
        self,
        project: Project,
        *,
        ui_config: UIConfig | None = None,
        config_path: Path | None = None,
        default_model: str | None = None,
    ) -> None:
        super().__init__()
        self._project = project
        self._ui_config = ui_config or UIConfig.load(config_path)
        # ``config_path`` lets the test suite redirect ``UIConfig.save``
        # to a tmp file. Production code passes ``None`` and lands at
        # ``default_config_path()``.
        self._config_path = config_path
        self._default_model = default_model

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with VerticalScroll(id="settings-body"):
            with Vertical(classes="panel", id="settings-project"):
                yield Static("Project", classes="panel-title")
                yield Static(self._project_block(), id="settings-project-body")
            with Vertical(classes="panel", id="settings-style"):
                yield Static(
                    "Style guide  [dim](press [/dim][b]E[/b][dim] to edit)[/dim]",
                    classes="panel-title",
                    markup=True,
                )
                yield Static(self._style_block(), id="settings-style-body", markup=True)
            with Vertical(classes="panel", id="settings-llm"):
                yield Static("LLM endpoint", classes="panel-title")
                yield Static(self._llm_block(), id="settings-llm-body", markup=False)
            with Vertical(classes="panel", id="settings-ui"):
                yield Static("UI preferences", classes="panel-title")
                yield Static(self._ui_block(), id="settings-ui-body")
            with Vertical(classes="panel", id="settings-help"):
                yield Static("How to change settings", classes="panel-title")
                yield Static(self._help_block(), id="settings-help-body")
        yield Static(
            "E to edit style · A to toggle auto tone-sniff · "
            "B (Dashboard) for budget · T for theme.",
            id="settings-status",
        )
        yield Footer()

    def on_mount(self) -> None:
        self._refresh()

    def action_refresh(self) -> None:
        self._refresh()
        self._set_status("Refreshed.")

    def _set_status(self, message: str) -> None:
        self.query_one("#settings-status", Static).update(message)

    def _refresh(self) -> None:
        self.query_one("#settings-project-body", Static).update(self._project_block())
        self.query_one("#settings-style-body", Static).update(self._style_block())
        self.query_one("#settings-llm-body", Static).update(self._llm_block())
        self.query_one("#settings-ui-body", Static).update(self._ui_block())
        self.query_one("#settings-help-body", Static).update(self._help_block())

    def action_edit_style(self) -> None:
        modal = StyleEditModal(
            current_profile=self._project.style_profile,
            current_text=self._project.style_guide,
        )
        self.app.push_screen(modal, self._on_style_edited)

    def _on_style_edited(self, result: StyleEditResult | None) -> None:
        if result is None:
            self._set_status("Style guide unchanged.")
            return
        self._project.update_style(
            style_profile=result.profile, custom_text=result.custom_text
        )
        self._refresh()
        self._set_status(
            "Style guide cleared."
            if result.profile is None and not result.custom_text
            else f"Style guide set: {label_for(result.profile)}."
        )

    def _project_block(self) -> str:
        from epublate.db import repo

        row = repo.get_project(self._project.engine, self._project.project_id)
        budget = (
            f"${row.budget_usd:.4f}" if row and row.budget_usd is not None else "(none)"
        )
        return (
            f"  name        : {self._project.name}\n"
            f"  source lang : {self._project.source_lang}\n"
            f"  target lang : {self._project.target_lang}\n"
            f"  source epub : {self._project.original_epub_path}\n"
            f"  database    : {self._project.db_path}\n"
            f"  budget cap  : {budget}"
        )

    def _style_block(self) -> str:
        """Render the active tone preset + a short prompt-block preview.

        Show enough of the prompt to confirm the curator chose what they
        meant (~160 chars of the first paragraph) without dumping the
        entire block onto the screen — :class:`StyleEditModal` is one
        keystroke away for the verbatim text.
        """

        profile = self._project.style_profile
        guide = self._project.style_guide
        label = label_for(profile)
        if guide is None:
            return (
                f"  preset  : {label}\n"
                "  prompt  : (no style guide — translator runs without one)\n"
                "  status  : OK to leave empty; press [b]E[/b] to add one."
            )
        first_paragraph = _first_paragraph(guide)
        preview = first_paragraph[:160] + ("…" if len(first_paragraph) > 160 else "")
        marker = " (custom prose)" if profile is None else ""
        return (
            f"  preset  : {label}{marker}\n"
            f"  preview : {preview}\n"
            f"  size    : {len(guide)} chars in the system prompt"
        )

    def _llm_block(self) -> str:
        provider = os.environ.get(ENV_PROVIDER, "(default: openai-compat)") or "(unset)"
        base_url = (
            os.environ.get(ENV_BASE_URL) or "(default: https://api.openai.com/v1)"
        )
        api_key = _redact_api_key(os.environ.get(ENV_API_KEY))
        model = (
            self._default_model
            or os.environ.get(ENV_MODEL)
            or "(unset — falls back to Reader default)"
        )
        helper = (
            os.environ.get(ENV_HELPER_MODEL) or "(unset — falls back to translator)"
        )
        org = os.environ.get(ENV_ORG) or "(unset)"
        return (
            f"  provider     : {provider}\n"
            f"  base url     : {base_url}\n"
            f"  api key      : {api_key}\n"
            f"  organization : {org}\n"
            f"  translator   : {model}\n"
            f"  helper       : {helper}"
        )

    def _ui_block(self) -> str:
        active_theme = self.app.theme if self.is_attached else "(detached)"
        saved_theme = self._ui_config.theme or "(default)"
        config_path = self._config_path or default_config_path()
        themes = ", ".join(EPUBLATE_THEME_ORDER)
        env_override = os.environ.get(ENV_AUTO_TONE_SNIFF, "").strip()
        effective = resolve_auto_tone_sniff(self._ui_config)
        # Make the env-var-wins case visible: if the env var is set and
        # disagrees with the persisted bool, the curator should know
        # *why* pressing A doesn't seem to take effect.
        sniff_line = f"  auto tone-sniff : {'on' if effective else 'off'}"
        if env_override:
            sniff_line += (
                f" [pinned by ${ENV_AUTO_TONE_SNIFF}={env_override}]"
                if effective != self._ui_config.auto_tone_sniff
                else f" [also via ${ENV_AUTO_TONE_SNIFF}={env_override}]"
            )
        return (
            f"  active theme    : {active_theme}\n"
            f"  saved theme     : {saved_theme}\n"
            f"  config file     : {config_path}\n"
            f"  cycle order     : {themes}\n"
            f"{sniff_line}"
        )

    # ------------------------------------------------------------------
    # Auto tone-sniff toggle (PRD F-STYLE-4)
    # ------------------------------------------------------------------

    def action_toggle_auto_tone_sniff(self) -> None:
        """Flip and persist :attr:`UIConfig.auto_tone_sniff`.

        We surface the env-var override case loudly: if
        ``EPUBLATE_AUTO_TONE_SNIFF`` is pinning a value, persistence
        still happens (so the bool is correct once the env var is
        unset) but the status row tells the curator their flip won't
        take effect this session.
        """

        new_value = not self._ui_config.auto_tone_sniff
        self._ui_config.auto_tone_sniff = new_value
        try:
            self._ui_config.save(self._config_path)
        except OSError as exc:
            _logger.warning("could not persist UI config: %s", exc)
            self._set_status(f"Could not persist toggle: {exc}")
            return
        self._refresh()
        env_override = os.environ.get(ENV_AUTO_TONE_SNIFF, "").strip()
        effective = resolve_auto_tone_sniff(self._ui_config)
        if env_override and effective != new_value:
            self._set_status(
                f"Auto tone-sniff saved as {'on' if new_value else 'off'}, but "
                f"${ENV_AUTO_TONE_SNIFF}={env_override} pins it "
                f"{'on' if effective else 'off'} this session."
            )
        else:
            self._set_status(f"Auto tone-sniff turned {'on' if new_value else 'off'}.")

    @staticmethod
    def _help_block() -> str:
        return (
            "  - Style guide   : press E to pick a tone preset or edit prose.\n"
            "  - Auto tone-sniff: press A to toggle helper-LLM detection on\n"
            "                    new project source picks (PRD F-STYLE-4).\n"
            "  - Budget cap    : open the Dashboard and press B.\n"
            "  - Theme         : press T anywhere to cycle dark / light / contrast.\n"
            "  - LLM config    : set EPUBLATE_LLM_BASE_URL / EPUBLATE_LLM_API_KEY /\n"
            "                    EPUBLATE_LLM_MODEL (and optionally\n"
            "                    EPUBLATE_LLM_HELPER_MODEL) before launching.\n"
            "  - Mock mode     : pass --mock-llm or set EPUBLATE_LLM=mock.\n"
            "  - Help          : press ? or F1 for the cheat sheet."
        )

    @property
    def is_attached(self) -> bool:
        # ``self.app`` raises before the screen is mounted; gate the
        # call so :meth:`_ui_block` is safe to invoke from ``compose``.
        try:
            _ = self.app
            return True
        except Exception:
            return False


__all__ = ["SettingsScreen", "StyleEditModal", "StyleEditResult"]
