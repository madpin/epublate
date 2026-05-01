"""In-TUI new-project modal (PRD §4.6 / §7.1 / F-STYLE-4).

Curators don't have to drop back to ``epublate new`` — pressing ``n``
on the Projects screen opens this modal which:

* asks for the source ePub path (with a one-key file browser so the
  curator doesn't have to paste a path),
* asks for target / source language, display name, and an optional
  output directory (defaulting to the user's *projects root*, with a
  collision-aware suggested folder),
* (PRD F-STYLE-4) once the source path settles, optionally fires a
  small **tone sniff** in the background — reads a spread of blocks
  from the picked ePub, asks the helper LLM for ``register`` /
  ``audience``, and pre-selects the matching tone preset (without
  overriding a manual pick the curator already made),
* validates the inputs in-place, surfacing specific errors next to
  the field that triggered them,
* on submit, calls :meth:`epublate.core.project.Project.create`,
  records the project in the recents store, and dismisses with the
  freshly-opened :class:`Project` handle.

The CLI flow continues to work for headless / scripted use; this is
the day-to-day curator path.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from rich.markup import escape as escape_markup
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, Rule, Select, Static, TextArea
from textual.worker import Worker

from epublate.app.branding import ICON_ARROW
from epublate.app.paths import (
    default_projects_root,
    ensure_projects_root,
    unique_project_dir,
    unique_project_name,
)
from epublate.app.recents import RecentsStore, record_project
from epublate.app.screens.file_browser import FileBrowserModal
from epublate.core.project import Project
from epublate.core.style import (
    DEFAULT_STYLE_PROFILE,
    PROFILE_REGISTRY,
    label_for,
    list_profiles,
)
from epublate.core.style_sniff import ToneSniff, sniff_tone
from epublate.errors import ConfigurationError

if TYPE_CHECKING:
    from epublate.llm.base import LLMProvider

ProviderFactory = Callable[[], "LLMProvider"]
"""Factory used by the modal to construct one helper-LLM provider per sniff.

Returning ``None`` (or raising) from the factory disables the sniff for
the current call without disturbing the modal — useful in environments
where ``EPUBLATE_LLM_MODEL`` isn't set."""


class _ToneSniffSucceeded(Message):
    """Posted by the sniff worker when the helper returned a parseable trace.

    Module-level (rather than nested in :class:`NewProjectModal`) so the
    ``@on`` decorator can dispatch on the type cleanly without depending
    on Textual's nested-class naming heuristics.
    """

    def __init__(self, result: ToneSniff) -> None:
        super().__init__()
        self.result = result


class _ToneSniffFailed(Message):
    """Posted by the sniff worker when the sniff couldn't complete.

    Reasons range from "no API key" (ConfigurationError) through
    "ePub failed to parse" to "helper returned garbage". The modal
    surfaces a one-line italic hint and otherwise carries on; the
    sniff is best-effort UX, not part of the create flow."""

    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason


_NONE_PROFILE_VALUE = "__none__"
"""Sentinel value used by the tone Select for the "no style guide" option.

Textual's ``Select`` insists on a hashable value per option, and we want
an explicit "opt out" entry separate from the curator simply not having
chosen anything yet. We translate this back to ``None`` before calling
:meth:`Project.create`."""

_logger = logging.getLogger(__name__)

# BCP-47-ish: 2-3 letter primary subtag, optional region/script subtags.
_LANG_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


class NewProjectModal(ModalScreen[Project | None]):
    """Modal for the ``new project`` flow (returns a :class:`Project` on success)."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel", "Cancel", show=True),
        Binding("ctrl+s", "submit", "Create", show=True),
        Binding("ctrl+o", "browse_source", "Browse ePub", show=True),
    ]

    DEFAULT_CSS = """
    NewProjectModal {
        align: center middle;
        background: $background 60%;
    }
    NewProjectModal #new-project-box {
        width: 90%;
        max-width: 110;
        height: auto;
        max-height: 90%;
        border: round $accent;
        padding: 1 2;
        background: $panel;
    }
    NewProjectModal #new-project-title {
        text-style: bold;
        color: $primary;
        padding: 0 0 0 0;
    }
    NewProjectModal #new-project-subtitle {
        color: $text-muted;
        padding: 0 0 1 0;
        height: 1;
    }
    NewProjectModal Rule {
        color: $accent 50%;
        margin: 0 0 1 0;
    }
    NewProjectModal .field-row {
        height: auto;
        padding: 0 0 0 0;
        margin: 0 0 1 0;
    }
    NewProjectModal .field-label {
        width: 18;
        padding: 1 1 0 0;
        color: $secondary;
        text-style: bold;
    }
    NewProjectModal .field-stack {
        width: 1fr;
        height: auto;
    }
    NewProjectModal .field-input-row {
        height: auto;
        padding: 0;
    }
    NewProjectModal .field-input-row Input {
        width: 1fr;
    }
    NewProjectModal .field-input-row Button {
        width: 12;
        height: 3;
        margin: 0 0 0 1;
    }
    NewProjectModal .field-hint {
        color: $text-muted;
        padding: 0 0 0 1;
        height: auto;
    }
    NewProjectModal Input {
        background: $surface;
    }
    NewProjectModal Input:focus {
        border: tall $accent;
    }
    NewProjectModal #new-project-error {
        color: $error;
        text-style: bold;
        padding: 1 0 0 0;
        height: auto;
    }
    NewProjectModal #new-project-buttons {
        height: 3;
        padding: 1 0 0 0;
        align-horizontal: right;
    }
    NewProjectModal #new-project-buttons Button {
        margin: 0 0 0 1;
    }
    NewProjectModal #new-project-hint {
        color: $text-muted;
        padding: 1 0 0 0;
        height: auto;
    }
    NewProjectModal Select {
        background: $surface;
    }
    NewProjectModal TextArea {
        height: 7;
        background: $surface;
        border: tall $primary 30%;
    }
    NewProjectModal TextArea:focus {
        border: tall $accent;
    }
    NewProjectModal #new-project-tone-row {
        height: auto;
        margin: 0 0 1 0;
    }
    """

    def __init__(
        self,
        *,
        default_source_lang: str = "en",
        default_target_lang: str = "pt",
        recents_path: Path | None = None,
        projects_root: Path | None = None,
        auto_tone_sniff: bool = False,
        tone_provider_factory: ProviderFactory | None = None,
        tone_helper_model: str | None = None,
    ) -> None:
        super().__init__()
        self._default_source_lang = default_source_lang
        self._default_target_lang = default_target_lang
        self._recents_path = recents_path
        self._projects_root = (
            projects_root.expanduser().resolve()
            if projects_root is not None
            else default_projects_root()
        )
        self._store = RecentsStore.load(self._recents_path)
        # Tone-sniff plumbing (PRD F-STYLE-4). The modal is dormant on
        # this front unless the caller wires *all three* of: the toggle,
        # a provider factory, and a helper model. The factory is held
        # lazily so we don't construct the OpenAI provider unless we
        # actually need one (saves env-var validation surprises).
        self._auto_tone_sniff = auto_tone_sniff
        self._tone_provider_factory = tone_provider_factory
        self._tone_helper_model = tone_helper_model
        # The Select fires ``Changed`` once on mount with its initial
        # value; we have to discard that event before we can use later
        # Changed events as "the curator picked something" signal,
        # otherwise auto-sniff would never override the (unchosen)
        # default. ``_select_seen_initial`` flips on the first Changed.
        self._select_seen_initial: bool = False
        self._suppress_tone_change_count: int = 0
        self._tone_was_user_set: bool = False
        self._last_sniffed_source: Path | None = None
        self._sniff_in_flight: bool = False

    def compose(self) -> ComposeResult:
        with Vertical(id="new-project-box"):
            yield Label(f"  {ICON_ARROW} New project", id="new-project-title")
            yield Label(
                "Bootstrap a project folder from a source ePub.",
                id="new-project-subtitle",
            )
            yield Rule(line_style="thick")
            yield from self._file_field(
                "Source ePub",
                "new-project-source",
                placeholder="/path/to/book.epub",
                hint=(
                    "press [b]Ctrl+O[/b] (or [b]Browse[/b]) to pick an .epub "
                    "from Downloads, Documents, or anywhere else"
                ),
                browse_id="new-project-source-browse",
            )
            yield from self._field(
                "Target language",
                "new-project-target-lang",
                value=self._default_target_lang,
                placeholder="pt",
                hint="BCP-47 code; e.g. pt, pt-BR, ja, fr",
            )
            yield from self._field(
                "Source language",
                "new-project-source-lang",
                value=self._default_source_lang,
                placeholder="en",
                hint="BCP-47 code; use 'und' if unknown",
            )
            yield from self._field(
                "Project name",
                "new-project-name",
                placeholder="(defaults to ePub stem; auto-dedups if taken)",
                hint="shown in the dashboard + recents list",
            )
            yield from self._tone_field()
            yield from self._file_field(
                "Output dir",
                "new-project-out",
                placeholder="(auto: <projects-root>/<stem>)",
                hint=self._output_hint_text(),
                browse_id="new-project-out-browse",
            )
            yield Static("", id="new-project-error", markup=False)
            yield Static(
                "[dim]Submit with [/][b]Ctrl+S[/][dim] · Cancel with [/][b]Esc[/]"
                "[dim] · Browse with [/][b]Ctrl+O[/]",
                id="new-project-hint",
                markup=True,
            )
            with Horizontal(id="new-project-buttons"):
                yield Button("Cancel", id="new-project-cancel", variant="default")
                yield Button(
                    "Create project",
                    id="new-project-submit",
                    variant="primary",
                )
        yield Footer()

    def _output_hint_text(self) -> str:
        root = escape_markup(str(self._projects_root))
        return (
            f"saved under [b]{root}[/b] by default — "
            "relative paths resolve there; set [b]EPUBLATE_PROJECTS_ROOT[/b] "
            "to change the default"
        )

    def _field(
        self,
        label: str,
        widget_id: str,
        *,
        value: str = "",
        placeholder: str = "",
        hint: str = "",
    ) -> ComposeResult:
        with Horizontal(classes="field-row"):
            yield Label(label, classes="field-label")
            with Vertical(classes="field-stack"):
                yield Input(value=value, placeholder=placeholder, id=widget_id)
                if hint:
                    yield Static(hint, classes="field-hint", markup=True)

    def _file_field(
        self,
        label: str,
        widget_id: str,
        *,
        placeholder: str,
        hint: str,
        browse_id: str,
    ) -> ComposeResult:
        with Horizontal(classes="field-row"):
            yield Label(label, classes="field-label")
            with Vertical(classes="field-stack"):
                with Horizontal(classes="field-input-row"):
                    yield Input(value="", placeholder=placeholder, id=widget_id)
                    yield Button("Browse…", id=browse_id, variant="default")
                yield Static(hint, classes="field-hint", markup=True)

    def _tone_field(self) -> ComposeResult:
        """Tone preset Select + editable prompt-block TextArea (PRD F-STYLE-1).

        The Select carries the preset slug; the TextArea is pre-populated
        with the preset's prompt block but stays editable so the curator
        can tweak it. On submit, an unmodified TextArea means "use the
        preset"; a modified one means "use this verbatim as
        ``style_guide`` and keep the slug for display only".

        A small status Static beneath the Select reports auto-sniff
        progress (PRD F-STYLE-4): "(detecting tone…)" while a sniff is
        in flight, "Helper suggests: …" on success, or empty when no
        sniff is configured.
        """

        options: list[tuple[str, str]] = [(p.name, p.id) for p in list_profiles()]
        options.append(("None — no style guide", _NONE_PROFILE_VALUE))
        with Vertical(classes="field-row", id="new-project-tone-row"):
            with Horizontal(classes="field-input-row"):
                yield Label("Tone", classes="field-label")
                yield Select(
                    options=options,
                    value=DEFAULT_STYLE_PROFILE,
                    allow_blank=False,
                    id="new-project-tone",
                )
            yield Static(
                "",
                id="new-project-tone-status",
                classes="field-hint",
                markup=True,
            )
            with Vertical(classes="field-stack"):
                yield TextArea(
                    text=PROFILE_REGISTRY[DEFAULT_STYLE_PROFILE].prompt_block,
                    id="new-project-tone-text",
                )
                yield Static(
                    "the LLM sees this verbatim — edit to fine-tune the voice "
                    "(PRD F-STYLE-1)",
                    classes="field-hint",
                    markup=True,
                )

    def on_mount(self) -> None:
        self.query_one("#new-project-source", Input).focus()

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "new-project-submit":
            self.action_submit()
            return
        if button_id == "new-project-cancel":
            self.action_cancel()
            return
        if button_id == "new-project-source-browse":
            self.action_browse_source()
            return
        if button_id == "new-project-out-browse":
            self._browse_output()
            return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        order = [
            "new-project-source",
            "new-project-target-lang",
            "new-project-source-lang",
            "new-project-name",
            "new-project-out",
        ]
        try:
            idx = order.index(event.input.id or "")
        except ValueError:
            return
        if event.input.id == "new-project-source":
            self._maybe_kick_off_tone_sniff(event.input.value.strip())
        if idx == len(order) - 1:
            self.action_submit()
            return
        next_input = self.query_one(f"#{order[idx + 1]}", Input)
        next_input.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id in {"new-project-source", "new-project-name"}:
            self._refresh_output_hint()

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id != "new-project-tone":
            return
        # Textual fires Select.Changed once on mount with the initial
        # value. We have to discard that event before we can use later
        # Changed events as a "the curator picked something" signal,
        # otherwise the auto-sniff suggestion would never replace the
        # default preset (PRD F-STYLE-4).
        first_change = not self._select_seen_initial
        self._select_seen_initial = True
        if not first_change and self._suppress_tone_change_count == 0:
            self._tone_was_user_set = True
        # Re-seed the TextArea with the freshly chosen preset so the
        # curator sees what the LLM will actually receive. If they want
        # the previous edit back, the modal Select is the only place
        # that triggers this — typing in the TextArea never replaces
        # the displayed text under them.
        text_area = self.query_one("#new-project-tone-text", TextArea)
        chosen = event.value
        if chosen == _NONE_PROFILE_VALUE:
            text_area.text = ""
            text_area.disabled = True
            return
        text_area.disabled = False
        if isinstance(chosen, str) and chosen in PROFILE_REGISTRY:
            text_area.text = PROFILE_REGISTRY[chosen].prompt_block

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_browse_source(self) -> None:
        source_raw = self.query_one("#new-project-source", Input).value.strip()
        start: Path | None = None
        if source_raw:
            candidate = Path(source_raw).expanduser()
            if candidate.is_file():
                start = candidate.parent
            elif candidate.is_dir():
                start = candidate
        modal = FileBrowserModal(
            mode="epub",
            start_path=start,
            title=f"  {ICON_ARROW} Pick source ePub",
            subtitle="Only .epub files are shown. Press Enter or double-click to pick.",
        )
        self.app.push_screen(modal, self._on_source_picked)

    def _browse_output(self) -> None:
        raw = self.query_one("#new-project-out", Input).value.strip()
        start: Path | None = None
        if raw:
            candidate = Path(raw).expanduser()
            if candidate.is_absolute():
                start = candidate if candidate.is_dir() else candidate.parent
        if start is None or not start.is_dir():
            start = self._projects_root if self._projects_root.is_dir() else None
        modal = FileBrowserModal(
            mode="directory",
            start_path=start,
            title=f"  {ICON_ARROW} Pick output folder",
            subtitle=(
                "Navigate to the parent folder, then press 'Pick this folder'. "
                "A subfolder named after the project will be created inside."
            ),
        )
        self.app.push_screen(modal, self._on_output_picked)

    def _on_source_picked(self, result: Path | None) -> None:
        if result is None:
            return
        self.query_one("#new-project-source", Input).value = str(result)
        self._refresh_output_hint()
        self._maybe_kick_off_tone_sniff(str(result))
        # Move focus along so the curator keeps flowing through the form.
        self.query_one("#new-project-target-lang", Input).focus()

    def _on_output_picked(self, result: Path | None) -> None:
        if result is None:
            return
        self.query_one("#new-project-out", Input).value = str(result)
        self._refresh_output_hint()

    # ------------------------------------------------------------------
    # Tone sniff (PRD F-STYLE-4)
    # ------------------------------------------------------------------

    def _maybe_kick_off_tone_sniff(self, raw_path: str) -> None:
        """Validate ``raw_path`` and start a sniff worker if appropriate.

        We bail early on any of the obvious "no point" conditions:

        * Auto-sniff toggle is off, or no provider/helper-model wired.
        * Path is empty / not a file / not an .epub.
        * We already sniffed *this exact path* — debounce so re-clicks
          on Browse with the same selection don't refire the helper.

        Failures inside the worker (no API key, parse error, etc.) are
        surfaced via :class:`_ToneSniffFailed` and reduced to a one-line
        hint so the curator still sees *something* but the modal never
        blocks on a tone sniff (PRD F-STYLE-4 / NFR-3).
        """

        if not (
            self._auto_tone_sniff
            and self._tone_provider_factory is not None
            and self._tone_helper_model
        ):
            return
        if not raw_path:
            return
        try:
            path = Path(raw_path).expanduser()
            if not path.is_absolute():
                path = path.resolve()
        except OSError:
            return
        if not path.is_file() or path.suffix.lower() != ".epub":
            return
        if self._last_sniffed_source == path:
            return
        self._last_sniffed_source = path
        self._sniff_in_flight = True
        self._set_tone_status(
            f"  detecting tone from [dim]{escape_markup(path.name)}[/dim] …"
        )
        target_lang = (
            self.query_one("#new-project-target-lang", Input).value.strip().lower()
            or self._default_target_lang
        )
        source_lang = (
            self.query_one("#new-project-source-lang", Input).value.strip().lower()
            or self._default_source_lang
        )
        self._tone_sniff_worker(
            path,
            source_lang=source_lang,
            target_lang=target_lang,
        )

    def _tone_sniff_worker(
        self,
        source_path: Path,
        *,
        source_lang: str,
        target_lang: str,
    ) -> Worker[None]:
        """Run :func:`sniff_tone` off the UI thread.

        Wrapped in a method (instead of decorating directly) so callers
        can choose between fire-and-forget and awaitable variants for
        tests; the production path always goes through ``run_worker``
        with ``thread=True`` because ``sniff_tone`` does blocking I/O
        (loading + parsing the ePub).
        """

        return self.run_worker(
            lambda: self._run_sniff_blocking(
                source_path,
                source_lang=source_lang,
                target_lang=target_lang,
            ),
            name="tone-sniff",
            group="tone-sniff",
            exclusive=True,
            thread=True,
        )

    def _run_sniff_blocking(
        self,
        source_path: Path,
        *,
        source_lang: str,
        target_lang: str,
    ) -> None:
        provider_factory = self._tone_provider_factory
        helper_model = self._tone_helper_model
        if provider_factory is None or not helper_model:
            return
        try:
            provider = provider_factory()
        except ConfigurationError as exc:
            self.post_message(_ToneSniffFailed(str(exc)))
            return
        except Exception as exc:  # never let a worker crash the modal
            _logger.debug("tone sniff: provider build failed: %s", exc)
            self.post_message(_ToneSniffFailed(str(exc)))
            return
        try:
            result = sniff_tone(
                epub_path=source_path,
                provider=provider,
                helper_model=helper_model,
                source_lang=source_lang,
                target_lang=target_lang,
            )
        except Exception as exc:
            _logger.debug("tone sniff: %s", exc)
            self.post_message(_ToneSniffFailed(str(exc)))
            return
        self.post_message(_ToneSniffSucceeded(result))

    @on(_ToneSniffSucceeded)
    def _handle_tone_sniff_succeeded(self, message: _ToneSniffSucceeded) -> None:
        self._sniff_in_flight = False
        sniff = message.result
        if sniff.profile is None:
            observed_bits: list[str] = []
            if sniff.register:
                observed_bits.append(f"register=[i]{escape_markup(sniff.register)}[/i]")
            if sniff.audience:
                observed_bits.append(f"audience=[i]{escape_markup(sniff.audience)}[/i]")
            observed = " · ".join(observed_bits) if observed_bits else "no clear signal"
            self._set_tone_status(
                f"  helper read {observed}; keeping your current pick "
                f"[dim](${sniff.cost_usd:.4f})[/dim]"
            )
            return
        suggested_label = label_for(sniff.profile)
        if self._tone_was_user_set:
            self._set_tone_status(
                f"  helper suggests: [b]{escape_markup(suggested_label)}[/b] "
                f"[dim]({sniff.profile})[/dim] · keeping your manual pick "
                f"[dim]${sniff.cost_usd:.4f}[/dim]"
            )
            return
        # Apply the suggestion programmatically. Suppress the Changed
        # tracker so we don't flip ``_tone_was_user_set`` and lock out
        # any future re-sniff if the curator changes the source.
        select = self.query_one("#new-project-tone", Select)
        if select.value != sniff.profile:
            with self._suppressed_tone_change():
                select.value = sniff.profile
        self._set_tone_status(
            f"  helper suggests: [b]{escape_markup(suggested_label)}[/b] "
            f"[dim]({sniff.profile})[/dim] · pre-selected "
            f"[dim](${sniff.cost_usd:.4f})[/dim]"
        )

    @on(_ToneSniffFailed)
    def _handle_tone_sniff_failed(self, message: _ToneSniffFailed) -> None:
        self._sniff_in_flight = False
        # Whisper rather than shout — failures here are best-effort UX,
        # not an error the curator should have to triage. The status
        # row is dim italic on purpose.
        self._set_tone_status(
            f"  [dim]tone sniff skipped: {escape_markup(message.reason)}[/dim]"
        )

    def _set_tone_status(self, message: str) -> None:
        widget = self.query_one("#new-project-tone-status", Static)
        widget.update(message)

    @contextmanager
    def _suppressed_tone_change(self) -> Iterator[None]:
        """Increment a counter while the tone Select changes programmatically.

        ``on_select_changed`` consults the counter to decide whether
        the change came from the curator (counter == 0) or from the
        sniff handler / our own bookkeeping (counter > 0). We don't
        unset ``_select_seen_initial`` here — the *initial* mount
        event has already been seen and consumed by the time any
        sniff fires.
        """

        self._suppress_tone_change_count += 1
        try:
            yield
        finally:
            self._suppress_tone_change_count -= 1

    def action_submit(self) -> None:
        try:
            project = self._create_project_from_form()
        except _FormError as exc:
            self._set_error(str(exc))
            return
        except ConfigurationError as exc:
            self._set_error(str(exc))
            return
        except Exception as exc:  # surface anything else gracefully
            _logger.exception("unexpected error creating project")
            self._set_error(f"unexpected error: {exc}")
            return
        record_project(
            project_dir=project.project_dir,
            name=project.name,
            source_lang=project.source_lang,
            target_lang=project.target_lang,
            path=self._recents_path,
        )
        self.dismiss(project)

    # ------------------------------------------------------------------
    # Form → Project
    # ------------------------------------------------------------------

    def _create_project_from_form(self) -> Project:
        source_raw = self._value("new-project-source").strip()
        target_lang = self._value("new-project-target-lang").strip().lower()
        source_lang = self._value("new-project-source-lang").strip().lower() or "und"
        name_raw = self._value("new-project-name").strip()
        out_raw = self._value("new-project-out").strip()

        if not source_raw:
            raise _FormError("Source ePub path is required.")
        source = Path(source_raw).expanduser()
        if not source.is_absolute():
            source = (Path.cwd() / source).resolve()
        else:
            source = source.resolve()
        if not source.is_file():
            raise _FormError(f"Source ePub not found: {source}")
        if source.suffix.lower() != ".epub":
            raise _FormError("Source must be a .epub file.")

        if not target_lang:
            raise _FormError("Target language is required.")
        if not _LANG_RE.fullmatch(target_lang):
            raise _FormError(
                f"Target language {target_lang!r} doesn't look like a BCP-47 code."
            )
        if not _LANG_RE.fullmatch(source_lang):
            raise _FormError(
                f"Source language {source_lang!r} doesn't look like a BCP-47 code."
            )

        tone_select = self.query_one("#new-project-tone", Select)
        tone_text_area = self.query_one("#new-project-tone-text", TextArea)
        raw_profile = tone_select.value
        if raw_profile == _NONE_PROFILE_VALUE or raw_profile is None:
            chosen_profile: str | None = None
        elif isinstance(raw_profile, str) and raw_profile in PROFILE_REGISTRY:
            chosen_profile = raw_profile
        else:
            chosen_profile = DEFAULT_STYLE_PROFILE
        custom_text_input = tone_text_area.text
        # If the curator left the TextArea identical to the preset's
        # prompt block, treat that as "use the preset" so the project
        # doesn't get pinned to a snapshot of today's wording.
        custom_text_for_create: str | None
        if chosen_profile is None:
            custom_text_for_create = custom_text_input.strip() or None
        else:
            preset_block = PROFILE_REGISTRY[chosen_profile].prompt_block
            custom_text_for_create = (
                None
                if custom_text_input.strip() == preset_block.strip()
                else custom_text_input
            )

        ensure_projects_root(self._projects_root)
        stem = source.stem or "project"
        if out_raw:
            out_candidate = Path(out_raw).expanduser()
            if not out_candidate.is_absolute():
                out_candidate = (self._projects_root / out_raw).resolve()
            else:
                out_candidate = out_candidate.resolve()
            # If the curator typed an existing, populated folder, auto-
            # disambiguate under its parent so they don't get the raw
            # "refusing to overwrite" error.
            out_dir = (
                unique_project_dir(out_candidate.parent, out_candidate.name)
                if self._looks_occupied(out_candidate)
                else out_candidate
            )
        else:
            out_dir = unique_project_dir(self._projects_root, stem)

        display_name = unique_project_name(
            name_raw or stem,
            existing_names=[e.name for e in self._store.entries if e.exists()],
        )

        return Project.create(
            source,
            out_dir=out_dir,
            source_lang=source_lang,
            target_lang=target_lang,
            name=display_name,
            style_profile=chosen_profile,
            style_guide=custom_text_for_create,
        )

    @staticmethod
    def _looks_occupied(path: Path) -> bool:
        try:
            return path.exists() and any(path.iterdir())
        except OSError:
            return True

    def _refresh_output_hint(self) -> None:
        """Keep the "Output dir" hint in sync with the current form state.

        Shows where the project *will actually land* once the curator
        submits — makes the projects-root + suffix logic visible instead
        of surprising them at submit time.
        """

        source_raw = self.query_one("#new-project-source", Input).value.strip()
        out_raw = self.query_one("#new-project-out", Input).value.strip()
        stem_hint = ""
        if source_raw:
            try:
                source = Path(source_raw).expanduser()
                stem = source.stem or "project"
                stem_hint = stem
            except OSError:
                stem_hint = ""
        if out_raw:
            candidate = Path(out_raw).expanduser()
            if not candidate.is_absolute():
                candidate = self._projects_root / out_raw
            suggestion = candidate
            if self._looks_occupied(candidate):
                suggestion = unique_project_dir(candidate.parent, candidate.name)
            hint = f"will create: [b]{escape_markup(str(suggestion))}[/b]" + (
                f"  [dim](auto-dedup from {escape_markup(candidate.name)})[/dim]"
                if suggestion != candidate
                else ""
            )
        elif stem_hint:
            candidate = unique_project_dir(self._projects_root, stem_hint)
            hint = f"will create: [b]{escape_markup(str(candidate))}[/b]"
        else:
            hint = self._output_hint_text()
        # The hint Static is always the last child of the Output field's
        # stack; find it by walking from the Input widget.
        input_widget = self.query_one("#new-project-out", Input)
        stack = input_widget.parent.parent if input_widget.parent else None
        if stack is None:
            return
        for child in stack.children:
            if isinstance(child, Static) and "field-hint" in child.classes:
                child.update(hint)
                return

    def _value(self, widget_id: str) -> str:
        return self.query_one(f"#{widget_id}", Input).value

    def _set_error(self, message: str) -> None:
        self.query_one("#new-project-error", Static).update(message)


class _FormError(Exception):
    """Raised when a form field fails validation."""


__all__ = ["NewProjectModal", "ProviderFactory"]
