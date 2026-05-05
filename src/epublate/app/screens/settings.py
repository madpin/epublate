"""Settings screen — tabbed layout with editable per-project + per-machine prefs.

Layout (PRD §4.6 / M6):

* **Project** — editable name, language pair (read-only), budget cap,
  style guide modal entry.
* **LLM** — per-project overrides for base URL, translator model, helper
  model. The API key stays env-only (invariant 5) and is shown
  redacted; we never persist it on the project row.
* **UI** — theme dropdown (cycles with ``T`` globally), auto tone-sniff
  toggle (PRD F-STYLE-4), config file path.
* **Intake** — global defaults for the helper-LLM intake pass: helper
  model, max segments, "run after new project" preference. Persists to
  :class:`~epublate.app.config.UIConfig` so the IntakeModal and
  NewProjectModal both pick them up automatically.
* **Concurrency** — global defaults for the batch worker: concurrency,
  retries.

The screen reads its in-memory :class:`UIConfig` snapshot at
construction time; the Dashboard now passes both ``ui_config`` and
``config_path`` so an edit on the Settings screen round-trips back
to the Dashboard's view (previously the Dashboard re-loaded UIConfig
from disk on close, which would silently drop unsaved tweaks).

Backward-compatibility: the read-only Static IDs (``settings-project-body``,
``settings-llm-body``, ``settings-style-body``, ``settings-ui-body``,
``settings-help-body``) are preserved so the existing snapshot and pilot
tests continue to query them by id even after the move into tab panes.
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
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    TabbedContent,
    TabPane,
    TextArea,
)

from epublate.app.config import (
    ENV_AUTO_TONE_SNIFF,
    UIConfig,
    default_config_path,
    resolve_auto_tone_sniff,
)
from epublate.app.themes import EPUBLATE_THEME_ORDER
from epublate.app.widgets import BatchStatusBar
from epublate.core.project import Project
from epublate.core.style import (
    DEFAULT_STYLE_PROFILE,
    PROFILE_REGISTRY,
    label_for,
    list_profiles,
)
from epublate.db import repo
from epublate.db.schema import AttachedLoreMode
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
    """Configuration overview + tabbed editor (PRD §4.6 / M6 / F-STYLE-2)."""

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
        padding: 0 1 0 1;
    }
    SettingsScreen TabbedContent {
        height: 1fr;
    }
    SettingsScreen TabPane {
        padding: 1 2;
    }
    SettingsScreen .panel-title {
        text-style: bold;
        padding: 0 0 1 0;
        color: $primary;
    }
    SettingsScreen .panel-subtitle {
        color: $text-muted;
        padding: 0 0 1 0;
    }
    SettingsScreen .field-row {
        height: auto;
        padding: 0 0 1 0;
    }
    SettingsScreen .field-row Label {
        width: 22;
        content-align-vertical: middle;
        padding: 1 1 0 0;
    }
    SettingsScreen .field-row Input {
        width: 1fr;
    }
    SettingsScreen .field-row Select {
        width: 1fr;
        background: $surface;
    }
    SettingsScreen .summary-block {
        height: auto;
        margin: 1 0 1 0;
        padding: 1 1;
        border: round $primary 50%;
        background: $boost;
    }
    SettingsScreen .panel-actions {
        height: 3;
        padding: 1 0 0 0;
        align-horizontal: right;
    }
    SettingsScreen .panel-actions Button {
        margin: 0 0 0 1;
    }
    SettingsScreen .info-banner {
        height: auto;
        padding: 1 1;
        margin: 0 0 1 0;
        border: round $accent;
        background: $boost;
        color: $text;
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

    @property
    def ui_config(self) -> UIConfig:
        """Expose the in-memory snapshot so the Dashboard can re-read it."""

        return self._ui_config

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        with (
            Vertical(id="settings-body"),
            TabbedContent(id="settings-tabs", initial="tab-project"),
        ):
            yield from self._compose_project_tab()
            yield from self._compose_llm_tab()
            yield from self._compose_ui_tab()
            yield from self._compose_intake_tab()
            yield from self._compose_concurrency_tab()
            yield from self._compose_lore_tab()
            yield from self._compose_help_tab()
        yield Static(
            "E to edit style · A to toggle auto tone-sniff · "
            "Tab to switch panels · click a Save button to persist edits.",
            id="settings-status",
        )
        yield BatchStatusBar(id="settings-batch-status")
        yield Footer()

    # ------------------------------------------------------------------
    # Tab compose helpers
    # ------------------------------------------------------------------

    def _compose_project_tab(self) -> ComposeResult:
        with TabPane("Project", id="tab-project"):
            yield Label("Project metadata", classes="panel-title")
            yield Label(
                "Edit the project name and budget cap. Languages are pinned at "
                "creation time — start a new project to switch them.",
                classes="panel-subtitle",
            )
            with Horizontal(classes="field-row"):
                yield Label("Display name:")
                yield Input(
                    value=self._project.name,
                    placeholder="Project name",
                    id="settings-project-name",
                )
            with Horizontal(classes="field-row"):
                yield Label("Budget USD (blank = none):")
                yield Input(
                    value=self._initial_budget_text(),
                    placeholder="e.g. 5.00",
                    id="settings-project-budget",
                )
            yield Label(
                "Preceding-segment context (default for batch + Reader; "
                "0 disables). Surfaces N preceding source/target pairs "
                "in the translator's prompt so a chatty chapter can keep "
                "its turns in flow. The char cap drops the oldest "
                "segments that exceed it (a segment is never split).",
                classes="panel-subtitle",
            )
            ctx_seg, ctx_chars = self._initial_context_text()
            with Horizontal(classes="field-row"):
                yield Label("Context segments:")
                yield Input(
                    value=ctx_seg,
                    placeholder="0",
                    id="settings-project-context-segments",
                )
            with Horizontal(classes="field-row"):
                yield Label("Context char cap:")
                yield Input(
                    value=ctx_chars,
                    placeholder="0",
                    id="settings-project-context-chars",
                )
            with Horizontal(classes="panel-actions"):
                yield Button(
                    "Save project",
                    id="settings-project-save",
                    variant="primary",
                )
            with Vertical(classes="summary-block"):
                yield Static(
                    "Current values",
                    classes="panel-subtitle",
                )
                yield Static(self._project_block(), id="settings-project-body")

            yield Label("Style guide", classes="panel-title")
            yield Label(
                "Press [b]E[/b] to pick a tone preset or write your own paragraph. "
                "Changes invalidate the translator cache automatically.",
                classes="panel-subtitle",
                markup=True,
            )
            with Vertical(classes="summary-block"):
                yield Static(
                    self._style_block(),
                    id="settings-style-body",
                    markup=True,
                )

    def _compose_llm_tab(self) -> ComposeResult:
        with TabPane("LLM", id="tab-llm"):
            yield Label("LLM endpoint overrides", classes="panel-title")
            yield Label(
                "These override the environment defaults for this project only. "
                "Leave a field blank to fall back to the matching env variable.",
                classes="panel-subtitle",
            )
            redacted = _redact_api_key(os.environ.get(ENV_API_KEY))
            yield Static(
                f"  API key stays env-only. Set [b]${ENV_API_KEY}[/b] in your "
                f"shell or `.env`. Current: [b]{redacted}[/b].",
                classes="info-banner",
                markup=True,
            )
            overrides = self._project_overrides()
            with Horizontal(classes="field-row"):
                yield Label("Base URL:")
                yield Input(
                    value=str(overrides.get("base_url", "") or ""),
                    placeholder=os.environ.get(ENV_BASE_URL)
                    or "https://api.openai.com/v1",
                    id="settings-llm-base-url",
                )
            with Horizontal(classes="field-row"):
                yield Label("Translator model:")
                yield Input(
                    value=str(overrides.get("translator_model", "") or ""),
                    placeholder=self._default_model
                    or os.environ.get(ENV_MODEL)
                    or "gpt-5-mini",
                    id="settings-llm-translator-model",
                )
            with Horizontal(classes="field-row"):
                yield Label("Helper model:")
                yield Input(
                    value=str(overrides.get("helper_model", "") or ""),
                    placeholder=os.environ.get(ENV_HELPER_MODEL)
                    or "(falls back to translator)",
                    id="settings-llm-helper-model",
                )
            with Horizontal(classes="panel-actions"):
                yield Button(
                    "Clear overrides",
                    id="settings-llm-clear",
                    variant="default",
                )
                yield Button(
                    "Save LLM",
                    id="settings-llm-save",
                    variant="primary",
                )
            with Vertical(classes="summary-block"):
                yield Static("Effective values", classes="panel-subtitle")
                yield Static(self._llm_block(), id="settings-llm-body")

    def _compose_ui_tab(self) -> ComposeResult:
        with TabPane("UI", id="tab-ui"):
            yield Label("Interface preferences", classes="panel-title")
            yield Label(
                "Theme persists to ~/.config/epublate/ui.toml. "
                "Press [b]T[/b] anywhere to cycle through the order shown below.",
                classes="panel-subtitle",
                markup=True,
            )
            theme_options: list[tuple[str, str]] = [
                (t, t) for t in EPUBLATE_THEME_ORDER
            ]
            current_theme = self._current_theme()
            with Horizontal(classes="field-row"):
                yield Label("Theme:")
                yield Select(
                    options=theme_options,
                    value=current_theme
                    if current_theme in EPUBLATE_THEME_ORDER
                    else EPUBLATE_THEME_ORDER[0],
                    allow_blank=False,
                    id="settings-ui-theme",
                )
            with Horizontal(classes="panel-actions"):
                yield Button(
                    "Save theme",
                    id="settings-ui-save",
                    variant="primary",
                )
            with Vertical(classes="summary-block"):
                yield Static("Current state", classes="panel-subtitle")
                yield Static(self._ui_block(), id="settings-ui-body")

    def _compose_intake_tab(self) -> ComposeResult:
        with TabPane("Intake", id="tab-intake"):
            yield Label("Intake defaults", classes="panel-title")
            yield Label(
                "These pre-fill the IntakeModal and the New Project modal. "
                "[b]Intake[/b] = scan source text for proper nouns and seed "
                "the lore bible. No translation happens; just helper-LLM "
                "extraction.",
                classes="panel-subtitle",
                markup=True,
            )
            with Horizontal(classes="field-row"):
                yield Label("Helper model:")
                yield Input(
                    value=self._ui_config.intake_helper_model or "",
                    placeholder=os.environ.get(ENV_HELPER_MODEL)
                    or "(falls back to translator)",
                    id="settings-intake-helper-model",
                )
            with Horizontal(classes="field-row"):
                yield Label("Max segments:")
                yield Input(
                    value=str(self._ui_config.intake_max_segments),
                    placeholder="30",
                    id="settings-intake-max-segments",
                )
            with Horizontal(classes="field-row"):
                yield Label("Run after new project:")
                yield Input(
                    value="y" if self._ui_config.intake_run_after_new else "n",
                    id="settings-intake-run-after",
                )
            with Horizontal(classes="panel-actions"):
                yield Button(
                    "Save intake",
                    id="settings-intake-save",
                    variant="primary",
                )
            with Vertical(classes="summary-block"):
                yield Static("Current defaults", classes="panel-subtitle")
                yield Static(self._intake_block(), id="settings-intake-body")

    def _compose_concurrency_tab(self) -> ComposeResult:
        with TabPane("Concurrency", id="tab-concurrency"):
            yield Label("Batch defaults", classes="panel-title")
            yield Label(
                "Pre-fills the BatchModal. Higher concurrency speeds up large "
                "batches at the cost of more in-flight LLM tokens (and rate-limit "
                "exposure).",
                classes="panel-subtitle",
            )
            with Horizontal(classes="field-row"):
                yield Label("Default concurrency:")
                yield Input(
                    value=str(self._ui_config.batch_concurrency),
                    placeholder="1",
                    id="settings-concurrency-batch",
                )
            with Horizontal(classes="field-row"):
                yield Label("Default retry count:")
                yield Input(
                    value=str(self._ui_config.batch_retries),
                    placeholder="1",
                    id="settings-concurrency-retries",
                )
            with Horizontal(classes="panel-actions"):
                yield Button(
                    "Save concurrency",
                    id="settings-concurrency-save",
                    variant="primary",
                )
            with Vertical(classes="summary-block"):
                yield Static("Current defaults", classes="panel-subtitle")
                yield Static(self._concurrency_block(), id="settings-concurrency-body")

    def _compose_lore_tab(self) -> ComposeResult:
        with TabPane("Lore Books", id="tab-lore"):
            yield Label("Attached Lore Books", classes="panel-title")
            yield Label(
                "Lore Books are portable lore bibles you can attach to this "
                "project. Their [b]locked[/b] and [b]confirmed[/b] entries "
                "merge into the translator's view (own > attached). A "
                "[b]writable[/b] Lore Book also receives newly auto-proposed "
                "entries during batch runs.",
                classes="panel-subtitle",
                markup=True,
            )
            with Horizontal(classes="field-row"):
                yield Label("Lore Book path:")
                yield Input(
                    value="",
                    id="settings-lore-path",
                    placeholder="/path/to/whatever.epublate-lore",
                )
            with Horizontal(classes="field-row"):
                yield Label("Mode:")
                yield Select(
                    options=[
                        (AttachedLoreMode.READ_ONLY, AttachedLoreMode.READ_ONLY),
                        (AttachedLoreMode.WRITABLE, AttachedLoreMode.WRITABLE),
                    ],
                    value=AttachedLoreMode.READ_ONLY,
                    allow_blank=False,
                    id="settings-lore-mode",
                )
            with Horizontal(classes="panel-actions"):
                yield Button(
                    "Detach selected",
                    id="settings-lore-detach",
                    variant="warning",
                )
                yield Button(
                    "Toggle mode",
                    id="settings-lore-toggle-mode",
                    variant="default",
                )
                yield Button(
                    "Move up",
                    id="settings-lore-move-up",
                    variant="default",
                )
                yield Button(
                    "Move down",
                    id="settings-lore-move-down",
                    variant="default",
                )
                yield Button(
                    "Attach",
                    id="settings-lore-attach",
                    variant="primary",
                )
            with Vertical(classes="summary-block"):
                yield Static(
                    "Currently attached (lower priority = applied first)",
                    classes="panel-subtitle",
                )
                table: DataTable[str] = DataTable(
                    id="settings-lore-table",
                    zebra_stripes=True,
                    cursor_type="row",
                    show_header=True,
                )
                table.add_columns("Pri", "Mode", "Path")
                yield table

    def _compose_help_tab(self) -> ComposeResult:
        with TabPane("Help", id="tab-help"):
            yield Label("How to change settings", classes="panel-title")
            with VerticalScroll():
                yield Static(self._help_block(), id="settings-help-body")

    # ------------------------------------------------------------------
    # Mount + refresh
    # ------------------------------------------------------------------

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
        self.query_one("#settings-intake-body", Static).update(self._intake_block())
        self.query_one("#settings-concurrency-body", Static).update(
            self._concurrency_block()
        )
        self.query_one("#settings-help-body", Static).update(self._help_block())
        self._refresh_lore_table()

    def _refresh_lore_table(self) -> None:
        table = self.query_one("#settings-lore-table", DataTable)
        table.clear()
        for row in self._attached_lore_rows():
            mode_label = (
                "[b green]writable[/]"
                if row.mode == AttachedLoreMode.WRITABLE
                else "[dim]read-only[/]"
            )
            table.add_row(
                str(row.priority),
                mode_label,
                row.lore_path,
                key=row.lore_path,
            )

    def _attached_lore_rows(self) -> list[repo.AttachedLoreRow]:
        return repo.list_attached_lore(
            self._project.engine, project_id=self._project.project_id
        )

    # ------------------------------------------------------------------
    # Style modal
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Save / reset button handling
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        handler = {
            "settings-project-save": self._save_project_panel,
            "settings-llm-save": self._save_llm_panel,
            "settings-llm-clear": self._clear_llm_panel,
            "settings-ui-save": self._save_ui_panel,
            "settings-intake-save": self._save_intake_panel,
            "settings-concurrency-save": self._save_concurrency_panel,
            "settings-lore-attach": self._attach_lore_book_panel,
            "settings-lore-detach": self._detach_lore_book_panel,
            "settings-lore-toggle-mode": self._toggle_lore_mode_panel,
            "settings-lore-move-up": self._move_lore_up_panel,
            "settings-lore-move-down": self._move_lore_down_panel,
        }.get(bid)
        if handler is None:
            return
        try:
            handler()
        except ValueError as exc:
            self._set_status(f"Could not save: {exc}")
        except OSError as exc:
            _logger.warning("settings save failed: %s", exc)
            self._set_status(f"Could not save: {exc}")

    def _save_project_panel(self) -> None:
        name_input = self.query_one("#settings-project-name", Input)
        budget_input = self.query_one("#settings-project-budget", Input)
        ctx_seg_input = self.query_one("#settings-project-context-segments", Input)
        ctx_chars_input = self.query_one("#settings-project-context-chars", Input)

        new_name = name_input.value.strip()
        if not new_name:
            self._set_status("Project name must not be blank.")
            return

        budget = self._parse_budget(budget_input.value.strip())
        ctx_segments = self._parse_non_negative_int(
            ctx_seg_input.value.strip(), label="Context segments"
        )
        ctx_chars = self._parse_non_negative_int(
            ctx_chars_input.value.strip(), label="Context char cap"
        )
        repo.update_project_name(
            self._project.engine, project_id=self._project.project_id, name=new_name
        )
        # Refresh the in-memory dataclass so other screens see the new name
        # without a round-trip through ``Project.open``.
        self._project.name = new_name
        repo.update_project_budget(
            self._project.engine,
            project_id=self._project.project_id,
            budget_usd=budget,
        )
        repo.update_project_context_defaults(
            self._project.engine,
            project_id=self._project.project_id,
            max_segments=ctx_segments,
            max_chars=ctx_chars,
        )
        self._refresh()
        self._set_status(
            f"Project saved: {new_name!r}; budget = "
            + ("(none)" if budget is None else f"${budget:.4f}")
            + f"; context = {ctx_segments} seg / {ctx_chars} chars"
        )

    def _save_llm_panel(self) -> None:
        base_url = self.query_one("#settings-llm-base-url", Input).value.strip()
        translator = self.query_one(
            "#settings-llm-translator-model", Input
        ).value.strip()
        helper = self.query_one("#settings-llm-helper-model", Input).value.strip()

        overrides: dict[str, object] = {}
        if base_url:
            overrides["base_url"] = base_url
        if translator:
            overrides["translator_model"] = translator
        if helper:
            overrides["helper_model"] = helper

        repo.set_llm_overrides(
            self._project.engine,
            project_id=self._project.project_id,
            overrides=overrides or None,
        )
        self._refresh()
        if overrides:
            keys = ", ".join(sorted(overrides))
            self._set_status(f"LLM overrides saved: {keys}.")
        else:
            self._set_status("LLM overrides cleared (env defaults will be used).")

    def _clear_llm_panel(self) -> None:
        for widget_id in (
            "#settings-llm-base-url",
            "#settings-llm-translator-model",
            "#settings-llm-helper-model",
        ):
            self.query_one(widget_id, Input).value = ""
        repo.set_llm_overrides(
            self._project.engine,
            project_id=self._project.project_id,
            overrides=None,
        )
        self._refresh()
        self._set_status("LLM overrides cleared (env defaults will be used).")

    def _save_ui_panel(self) -> None:
        select = self.query_one("#settings-ui-theme", Select)
        chosen = select.value
        if not isinstance(chosen, str) or not chosen:
            self._set_status("Pick a theme before saving.")
            return
        if chosen != self.app.theme:
            self.app.theme = chosen
        self._ui_config.theme = chosen
        self._ui_config.save(self._config_path)
        self._refresh()
        self._set_status(f"Theme saved: {chosen}.")

    def _save_intake_panel(self) -> None:
        helper = self.query_one("#settings-intake-helper-model", Input).value.strip()
        max_raw = self.query_one("#settings-intake-max-segments", Input).value.strip()
        run_raw = (
            self.query_one("#settings-intake-run-after", Input).value.strip().lower()
        )

        try:
            max_seg = int(max_raw) if max_raw else self._ui_config.intake_max_segments
        except ValueError as exc:
            raise ValueError("Max segments must be an integer.") from exc
        if max_seg < 1:
            raise ValueError("Max segments must be at least 1.")

        run_after = run_raw in {"y", "yes", "true", "1", "on"}

        self._ui_config.intake_helper_model = helper or None
        self._ui_config.intake_max_segments = max_seg
        self._ui_config.intake_run_after_new = run_after
        self._ui_config.save(self._config_path)
        self._refresh()
        self._set_status(
            f"Intake defaults saved: helper="
            f"{helper or '(unset)'}, max={max_seg}, "
            f"run-after-new={'on' if run_after else 'off'}."
        )

    def _save_concurrency_panel(self) -> None:
        concurrency_raw = self.query_one(
            "#settings-concurrency-batch", Input
        ).value.strip()
        retries_raw = self.query_one(
            "#settings-concurrency-retries", Input
        ).value.strip()

        try:
            concurrency = (
                int(concurrency_raw)
                if concurrency_raw
                else self._ui_config.batch_concurrency
            )
        except ValueError as exc:
            raise ValueError("Concurrency must be an integer.") from exc
        if concurrency < 1:
            raise ValueError("Concurrency must be at least 1.")

        try:
            retries = int(retries_raw) if retries_raw else self._ui_config.batch_retries
        except ValueError as exc:
            raise ValueError("Retries must be an integer.") from exc
        if retries < 0:
            raise ValueError("Retries must be zero or positive.")

        self._ui_config.batch_concurrency = concurrency
        self._ui_config.batch_retries = retries
        self._ui_config.save(self._config_path)
        self._refresh()
        self._set_status(
            f"Concurrency defaults saved: concurrency={concurrency}, retries={retries}."
        )

    # ------------------------------------------------------------------
    # Lore Books panel (PRD §4.3 / F-LB-10 phase 3)
    # ------------------------------------------------------------------

    def _selected_lore_path(self) -> str | None:
        table = self.query_one("#settings-lore-table", DataTable)
        if table.row_count == 0:
            return None
        try:
            row_index = int(table.cursor_row)
        except (AttributeError, TypeError, ValueError):
            return None
        rows = self._attached_lore_rows()
        if row_index < 0 or row_index >= len(rows):
            return None
        return rows[row_index].lore_path

    def _attach_lore_book_panel(self) -> None:
        path_input = self.query_one("#settings-lore-path", Input)
        mode_select = self.query_one("#settings-lore-mode", Select)
        raw_path = path_input.value.strip()
        if not raw_path:
            raise ValueError("Lore Book path must not be blank.")
        candidate = Path(raw_path).expanduser()
        # We accept paths that don't yet exist on disk so curators can
        # paste a planned location, but flag the case so the status
        # line stays informative. The pipeline revalidates at translate
        # time.
        if not candidate.is_dir():
            self._set_status(
                f"Attached {candidate} (not on disk yet — make sure the "
                "directory exists before the next batch run)."
            )
        resolved = candidate.resolve(strict=False)
        chosen_mode = (
            str(mode_select.value)
            if mode_select.value not in (None, Select.NULL)
            else AttachedLoreMode.READ_ONLY
        )
        repo.attach_lore_book(
            self._project.engine,
            project_id=self._project.project_id,
            lore_path=str(resolved),
            mode=chosen_mode,
        )
        path_input.value = ""
        self._refresh_lore_table()
        if candidate.is_dir():
            self._set_status(f"Attached Lore Book at {resolved} (mode={chosen_mode}).")

    def _detach_lore_book_panel(self) -> None:
        target = self._selected_lore_path()
        if target is None:
            self._set_status("No Lore Book selected to detach.")
            return
        removed = repo.detach_lore_book(
            self._project.engine,
            project_id=self._project.project_id,
            lore_path=target,
        )
        self._refresh_lore_table()
        if removed:
            self._set_status(f"Detached {target}.")
        else:
            self._set_status(f"Could not detach {target} — already removed?")

    def _toggle_lore_mode_panel(self) -> None:
        target = self._selected_lore_path()
        if target is None:
            self._set_status("No Lore Book selected to toggle.")
            return
        current = next(
            (r for r in self._attached_lore_rows() if r.lore_path == target),
            None,
        )
        if current is None:
            self._set_status(f"Could not find {target} on the attached list.")
            return
        new_mode = (
            AttachedLoreMode.READ_ONLY
            if current.mode == AttachedLoreMode.WRITABLE
            else AttachedLoreMode.WRITABLE
        )
        repo.update_attached_lore(
            self._project.engine,
            project_id=self._project.project_id,
            lore_path=target,
            mode=new_mode,
        )
        self._refresh_lore_table()
        self._set_status(f"{target} is now [b]{new_mode}[/].")

    def _move_lore_up_panel(self) -> None:
        self._reorder_lore(direction=-1)

    def _move_lore_down_panel(self) -> None:
        self._reorder_lore(direction=+1)

    def _reorder_lore(self, *, direction: int) -> None:
        target = self._selected_lore_path()
        if target is None:
            self._set_status("No Lore Book selected to reorder.")
            return
        rows = self._attached_lore_rows()
        try:
            idx = next(i for i, r in enumerate(rows) if r.lore_path == target)
        except StopIteration:
            self._set_status(f"Could not find {target} on the attached list.")
            return
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= len(rows):
            return
        # Renumber priorities so the moved row swaps with its neighbour.
        # We deliberately renumber the whole list afterwards (0..N-1) so
        # the priority column stays compact even after many shuffles.
        new_order = list(rows)
        new_order[idx], new_order[new_idx] = new_order[new_idx], new_order[idx]
        for new_priority, row in enumerate(new_order):
            repo.update_attached_lore(
                self._project.engine,
                project_id=self._project.project_id,
                lore_path=row.lore_path,
                priority=new_priority,
            )
        self._refresh_lore_table()
        self._set_status(
            "Reordered " + target + " " + ("up" if direction < 0 else "down") + "."
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

    # ------------------------------------------------------------------
    # Read-only summary blocks
    # ------------------------------------------------------------------

    def _project_block(self) -> str:
        row = repo.get_project(self._project.engine, self._project.project_id)
        budget = (
            f"${row.budget_usd:.4f}" if row and row.budget_usd is not None else "(none)"
        )
        ctx_segments = row.context_max_segments if row is not None else 0
        ctx_chars = row.context_max_chars if row is not None else 0
        ctx_segments_label = f"{ctx_segments}" if ctx_segments > 0 else "0 (off)"
        ctx_chars_label = f"{ctx_chars}" if ctx_chars > 0 else "0 (no cap)"
        return (
            f"  name             : {self._project.name}\n"
            f"  source lang      : {self._project.source_lang}\n"
            f"  target lang      : {self._project.target_lang}\n"
            f"  source epub      : {self._project.original_epub_path}\n"
            f"  database         : {self._project.db_path}\n"
            f"  budget cap       : {budget}\n"
            f"  context segments : {ctx_segments_label}\n"
            f"  context char cap : {ctx_chars_label}"
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
        overrides = self._project_overrides()
        base_url = (
            str(overrides.get("base_url"))
            if overrides.get("base_url")
            else os.environ.get(ENV_BASE_URL) or "(default: https://api.openai.com/v1)"
        )
        api_key = _redact_api_key(os.environ.get(ENV_API_KEY))
        translator_override = overrides.get("translator_model")
        model = (
            str(translator_override)
            if translator_override
            else self._default_model
            or os.environ.get(ENV_MODEL)
            or "(unset — falls back to Reader default)"
        )
        helper_override = overrides.get("helper_model")
        helper = (
            str(helper_override)
            if helper_override
            else os.environ.get(ENV_HELPER_MODEL)
            or "(unset — falls back to translator)"
        )
        org = os.environ.get(ENV_ORG) or "(unset)"
        # Use parentheses (not [brackets]) for the override marker so the
        # summary Static renders it verbatim — Static's default
        # ``markup=True`` would otherwise eat ``[override]`` as a rich
        # markup tag and leave a confusing blank space behind.
        suffix = " (override)" if overrides else ""
        return (
            f"  provider     : {provider}\n"
            f"  base url     : {base_url}"
            f"{' (override)' if overrides.get('base_url') else ''}\n"
            f"  api key      : {api_key}\n"
            f"  organization : {org}\n"
            f"  translator   : {model}"
            f"{' (override)' if translator_override else ''}\n"
            f"  helper       : {helper}"
            f"{' (override)' if helper_override else ''}\n"
            f"  overrides    : {'set' if overrides else 'none'}{suffix}"
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

    def _intake_block(self) -> str:
        helper = self._ui_config.intake_helper_model or "(falls back to translator)"
        env_helper = os.environ.get(ENV_HELPER_MODEL)
        if env_helper and not self._ui_config.intake_helper_model:
            helper = f"{env_helper}  [via ${ENV_HELPER_MODEL}]"
        return (
            f"  helper model        : {helper}\n"
            f"  max segments        : {self._ui_config.intake_max_segments}\n"
            f"  run after new proj  : "
            f"{'on' if self._ui_config.intake_run_after_new else 'off'}"
        )

    def _concurrency_block(self) -> str:
        return (
            f"  default concurrency : {self._ui_config.batch_concurrency}\n"
            f"  default retries     : {self._ui_config.batch_retries}"
        )

    def _project_overrides(self) -> dict[str, object]:
        return repo.get_llm_overrides(self._project.engine, self._project.project_id)

    def _initial_budget_text(self) -> str:
        row = repo.get_project(self._project.engine, self._project.project_id)
        if row is None or row.budget_usd is None:
            return ""
        return f"{row.budget_usd:.4f}"

    def _initial_context_text(self) -> tuple[str, str]:
        """Pre-fill values for the per-project preceding-segment context."""

        row = repo.get_project(self._project.engine, self._project.project_id)
        if row is None:
            return ("0", "0")
        return (str(row.context_max_segments), str(row.context_max_chars))

    def _current_theme(self) -> str:
        try:
            return str(self.app.theme)
        except Exception:  # screen not yet attached
            return self._ui_config.theme or EPUBLATE_THEME_ORDER[0]

    @staticmethod
    def _parse_budget(raw: str) -> float | None:
        if not raw:
            return None
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError("Budget must be numeric or blank.") from exc
        if value < 0:
            raise ValueError("Budget cannot be negative.")
        return value

    @staticmethod
    def _parse_non_negative_int(raw: str, *, label: str) -> int:
        """Parse a non-negative integer field; blank means ``0``.

        ``label`` shapes the error message so the curator gets the
        right field name (mirroring the budget parser's behaviour).
        """

        if not raw:
            return 0
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{label} must be an integer.") from exc
        if value < 0:
            raise ValueError(f"{label} cannot be negative.")
        return value

    @staticmethod
    def _help_block() -> str:
        # Wrap the multi-line block as several string literals; ruff E501
        # gets unhappy when long phrases like "Theme: dropdown / [b]T[/b]"
        # push past 88 columns.
        lines = [
            "  - Style guide      : press [b]E[/b] to pick a tone preset or",
            "                       edit prose verbatim.",
            "  - Auto tone-sniff  : press [b]A[/b] to toggle helper-LLM",
            "                       detection on new project source picks",
            "                       (PRD F-STYLE-4).",
            "  - Project name     : Project tab → edit field → Save.",
            "  - Budget cap       : Project tab → set USD → Save",
            "                       (or B on Dashboard).",
            "  - Context defaults : Project tab → preceding-segment",
            "                       max segments / chars → Save.",
            "  - LLM overrides    : LLM tab → optional per-project",
            "                       model / base URL.",
            "  - Theme            : UI tab dropdown, or press [b]T[/b]",
            "                       anywhere to cycle.",
            "  - Intake defaults  : Intake tab → helper, max segments,",
            "                       run-after-new.",
            "  - Concurrency      : Concurrency tab → batch concurrency",
            "                       / retries.",
            "  - LLM env vars     : EPUBLATE_LLM_BASE_URL /",
            "                       EPUBLATE_LLM_API_KEY /",
            "                       EPUBLATE_LLM_MODEL (and optionally",
            "                       EPUBLATE_LLM_HELPER_MODEL).",
            "  - Mock mode        : pass --mock-llm or set EPUBLATE_LLM=mock.",
            "  - Help             : press [b]?[/b] or F1 for the cheat sheet.",
        ]
        return "\n".join(lines)

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
