"""Top-level Textual app for epublate.

M0 boots straight into :class:`ProjectsScreen`. M2 adds the ability to land
on the Reader for a specific project when the app is launched via
``epublate open <project-dir>``. M6 finishes the chrome: a registered
high-contrast theme cycled with ``T``, a global help/cheat-sheet modal
on ``?`` / ``f1``, and persisted theme preferences under
``~/.config/epublate/ui.toml``.

The app also owns a tiny shared :class:`BatchProgress` slot the Dashboard
populates while a batch worker runs. The Reader reads it on its refresh
tick so curators can use the Reader as a live progress monitor while the
batch runs in the background — they see segments flip from
``pending`` → ``translated`` in place and a footer line summarizing the
batch (PRD §4.6 'live updates' / TUI rule §1).

The App also owns the **batch worker thread** itself (not the
Dashboard). That ownership move is what lets a curator queue a long
batch, navigate back to the Projects screen (or even open a *different*
project) and have the original batch keep running. The Dashboard
re-attaches to the running batch on mount; the App keeps a strong
reference to the originating ``Project`` so its SQLite engine stays
alive until the worker drains.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from textual import work
from textual.app import App
from textual.binding import Binding, BindingType
from textual.screen import Screen

from epublate.app.config import UIConfig
from epublate.app.log_buffer import RingBufferHandler, install_ring_buffer
from epublate.app.messages import BatchFinished, BatchPrePassTick, BatchTick
from epublate.app.screens.help import HelpScreen
from epublate.app.screens.projects import ProjectsScreen
from epublate.app.themes import (
    EPUBLATE_CONTRAST_THEME_NAME,
    EPUBLATE_DEFAULT_THEME_NAME,
    EPUBLATE_THEME_ORDER,
    epublate_contrast_theme,
    epublate_theme,
)
from epublate.core.batch import (
    BatchCancelled,
    BatchOptions,
    BatchPaused,
    BatchPrePassProgress,
    BatchProgressEvent,
    BatchSummary,
    run_batch,
)

if TYPE_CHECKING:
    from epublate.core.project import Project
    from epublate.llm.base import LLMProvider

_logger = logging.getLogger(__name__)


ProviderFactory = Callable[[], "LLMProvider"]


@dataclass(slots=True)
class BatchProgress:
    """Cross-screen snapshot of an in-flight batch run.

    The App owns the worker that mutates this; other screens
    (Dashboard, Reader, Inbox) read it to surface progress without
    coupling to the Dashboard's message bus. ``project_id`` is
    included so a Reader on a *different* project ignores progress
    that doesn't belong to it.

    ``chapter_count`` is the upfront number of distinct chapters the
    batch is touching (for the "translating 4 chapters" UI line); it
    pairs with ``total`` (the segment count) so curators can read both
    granularities at a glance.

    ``starting_spend_usd`` snapshots the project's total cost at the
    moment the batch began, so the Dashboard can render
    "this batch: $X · project total: $Y" without a second DB read.
    """

    active: bool = False
    project_id: str | None = None
    summary: BatchSummary | None = None
    total: int = 0
    chapter_count: int = 0
    starting_spend_usd: float = 0.0
    started_at: float | None = None
    paused: bool = False
    cancelling: bool = False


@dataclass(slots=True)
class _BatchHandle:
    """Internal: every state the App needs to keep a batch alive.

    Holding ``project`` here keeps the SQLite engine open through the
    worker's lifetime: even if every screen referencing the project
    closes (curator backed out to the Projects list, opened a
    different project), the engine stays usable until the worker
    drains.
    """

    project: Project
    cancel_event: threading.Event
    chapter_count: int
    total_segments: int
    started_at: float
    starting_spend_usd: float = 0.0


class EpublateApp(App[None]):
    """Textual application root."""

    TITLE = "epublate"
    SUB_TITLE = "translate ePub story books with an LLM"

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("ctrl+c", "quit", "Quit", show=False, priority=True),
        Binding("T", "cycle_theme", "Theme", show=True, priority=True),
        Binding("question_mark", "help", "Help", show=True, priority=True),
        Binding("f1", "help", "Help", show=False, priority=True),
    ]

    def __init__(
        self,
        *,
        initial_screen: Screen[None] | None = None,
        config_path: Path | None = None,
        ui_config: UIConfig | None = None,
        default_model: str | None = None,
    ) -> None:
        super().__init__()
        self._initial_screen = initial_screen
        self._config_path = config_path
        self._ui_config = ui_config or UIConfig.load(config_path)
        # Translator model the default landing screen should propagate
        # to the Dashboard. Resolved by the CLI from ``$EPUBLATE_LLM_MODEL``
        # (PRD F-LLM-1) so that ``epublate`` and ``epublate open`` agree
        # on which model the user picked.
        self._default_model = default_model
        self.batch_progress: BatchProgress = BatchProgress()
        self._batch_handle: _BatchHandle | None = None
        # Dashboard that originated the batch — receives BatchTick /
        # BatchFinished messages from the worker thread. May be None
        # if the curator navigated away; messages are silently
        # dropped in that case (the BatchProgress slot stays
        # authoritative for any screen that re-attaches later).
        self._batch_listener: Screen[None] | None = None
        # Ring-buffer handler that backs the Logs screen. Installed on
        # the package-root ``epublate`` logger so every child logger
        # (``epublate.core.batch``, ``epublate.llm.openai_compat`` …)
        # propagates into it without further wiring. The handler is
        # held on the App so a future "Clear logs" action / test
        # cleanup can reach it.
        self.log_buffer: RingBufferHandler = install_ring_buffer()

    def on_mount(self) -> None:
        self.register_theme(epublate_theme())
        self.register_theme(epublate_contrast_theme())
        chosen = self._ui_config.theme
        if chosen and chosen in self.available_themes:
            self.theme = chosen
        else:
            # Fresh installs land on the branded look. Cycling with `T`
            # always gets curators back to a stock theme in two keys.
            self.theme = EPUBLATE_DEFAULT_THEME_NAME
        screen: Screen[None] = self._initial_screen or ProjectsScreen(
            default_model=self._default_model,
            ui_config=self._ui_config,
        )
        self.push_screen(screen)

    def action_cycle_theme(self) -> None:
        try:
            idx = EPUBLATE_THEME_ORDER.index(self.theme)
        except ValueError:
            idx = -1
        next_theme = EPUBLATE_THEME_ORDER[(idx + 1) % len(EPUBLATE_THEME_ORDER)]
        self.theme = next_theme
        self._persist_theme(next_theme)

    def action_help(self) -> None:
        target = self.screen
        if isinstance(target, HelpScreen):
            return
        self.push_screen(HelpScreen(target_screen=target))

    def action_toggle_dark(self) -> None:
        # Backwards-compatible shim for the M0 binding (`d`) while the
        # tri-state cycler is the new canonical action; some tests still
        # press ``d`` and expect a flip between the two built-in themes.
        self.theme = "textual-light" if self.theme == "textual-dark" else "textual-dark"
        self._persist_theme(self.theme)

    def _persist_theme(self, name: str) -> None:
        self._ui_config.theme = name
        try:
            self._ui_config.save(self._config_path)
        except OSError as exc:
            _logger.warning("could not persist UI config: %s", exc)

    # ------------------------------------------------------------------
    # Batch ownership (PRD §7.3 / M4)
    # ------------------------------------------------------------------

    @property
    def batch_running(self) -> bool:
        return self._batch_handle is not None

    def is_batch_running_for(self, project_id: str) -> bool:
        """True iff the App's running batch belongs to ``project_id``."""

        return (
            self._batch_handle is not None
            and self._batch_handle.project.project_id == project_id
        )

    def attach_batch_listener(self, listener: Screen[None] | None) -> None:
        """Subscribe a Dashboard (or other Screen) to per-segment ticks.

        We keep at most one listener: only the Dashboard that owns
        the batch's lifecycle controls ever needs the messages.
        Other screens (Reader, Inbox) read the
        ``self.batch_progress`` slot on their own refresh tick.
        Passing ``None`` detaches whoever was previously listening
        (used when the Dashboard pops without cancelling).
        """

        self._batch_listener = listener

    def start_batch(
        self,
        *,
        project: Project,
        options: BatchOptions,
        chapter_count: int,
        total_segments: int,
        provider_factory: ProviderFactory,
        starting_spend_usd: float = 0.0,
        listener: Screen[None] | None = None,
    ) -> bool:
        """Kick off a batch run owned by the App.

        Returns ``False`` if a batch is already running (callers
        should refuse to dispatch a second one). On success,
        registers ``listener`` as the Dashboard to call back, sets
        the cross-screen :class:`BatchProgress` slot, and dispatches
        the worker.
        """

        if self._batch_handle is not None:
            return False
        cancel_event = threading.Event()
        self._batch_handle = _BatchHandle(
            project=project,
            cancel_event=cancel_event,
            chapter_count=chapter_count,
            total_segments=total_segments,
            started_at=time.monotonic(),
            starting_spend_usd=starting_spend_usd,
        )
        self._batch_listener = listener
        self.batch_progress = BatchProgress(
            active=True,
            project_id=project.project_id,
            summary=None,
            total=total_segments,
            chapter_count=chapter_count,
            starting_spend_usd=starting_spend_usd,
            started_at=self._batch_handle.started_at,
            paused=False,
            cancelling=False,
        )
        self._batch_worker(
            project_id=project.project_id,
            options=options,
            provider_factory=provider_factory,
        )
        return True

    def cancel_batch(self) -> bool:
        """Ask the running batch to stop; returns ``False`` if none was running.

        Cancellation is best-effort: in-flight LLM calls finish, no
        new ones are submitted. We flip the App-level
        ``BatchProgress.cancelling`` flag so the meter can show
        "cancelling…" while the in-flight calls drain.
        """

        if self._batch_handle is None:
            return False
        self._batch_handle.cancel_event.set()
        self.batch_progress = BatchProgress(
            active=self.batch_progress.active,
            project_id=self.batch_progress.project_id,
            summary=self.batch_progress.summary,
            total=self.batch_progress.total,
            chapter_count=self.batch_progress.chapter_count,
            starting_spend_usd=self.batch_progress.starting_spend_usd,
            started_at=self.batch_progress.started_at,
            paused=self.batch_progress.paused,
            cancelling=True,
        )
        return True

    @work(exclusive=True, group="batch", thread=True)
    def _batch_worker(
        self,
        *,
        project_id: str,
        options: BatchOptions,
        provider_factory: ProviderFactory,
    ) -> None:
        handle = self._batch_handle
        if handle is None or handle.project.project_id != project_id:
            return
        provider = provider_factory()
        try:
            summary = run_batch(
                engine=handle.project.engine,
                project_id=project_id,
                source_lang=handle.project.source_lang,
                target_lang=handle.project.target_lang,
                provider=provider,
                options=options,
                on_progress=self._post_batch_progress,
                on_pre_pass_progress=self._post_pre_pass_progress,
                cancel_event=handle.cancel_event,
            )
        except BatchPaused as paused:
            self._on_worker_done(
                status="paused", summary=paused.summary, project_id=project_id
            )
            return
        except BatchCancelled as cancelled:
            self._on_worker_done(
                status="cancelled",
                summary=cancelled.summary,
                project_id=project_id,
            )
            return
        except Exception as exc:
            _logger.exception("batch worker failed for %s", project_id)
            self._on_worker_done(
                status="failed",
                summary=None,
                project_id=project_id,
                error=str(exc),
            )
            return
        self._on_worker_done(status="completed", summary=summary, project_id=project_id)

    def _post_batch_progress(self, event: BatchProgressEvent) -> None:
        """Worker-thread callback: posts a tick to the listener (if any).

        Updates the App-level :class:`BatchProgress` *before* posting
        the message so any listener that reads the slot during the
        message handler sees the latest snapshot.
        """

        handle = self._batch_handle
        if handle is None:
            return
        self.batch_progress = BatchProgress(
            active=True,
            project_id=handle.project.project_id,
            summary=event.summary,
            total=handle.total_segments,
            chapter_count=handle.chapter_count,
            starting_spend_usd=handle.starting_spend_usd,
            started_at=handle.started_at,
            paused=False,
            cancelling=handle.cancel_event.is_set(),
        )
        listener = self._batch_listener
        if listener is not None:
            try:
                listener.post_message(BatchTick(event))
            except Exception:  # pragma: no cover — defensive
                _logger.debug("listener no longer accepts messages")

    def _post_pre_pass_progress(self, event: BatchPrePassProgress) -> None:
        """Worker-thread callback: posts a pre-pass tick to the listener.

        Pre-pass progress doesn't move the segment-count meter (the
        helper LLM doesn't translate segments) but it surfaces "the
        helper is alive" to the curator's status line, which kept
        bug-2026-05 alive long enough for users to think a pre-pass
        was a deadlock. The Dashboard reads ``event.chapter_index``
        / ``chunk_index`` to render "pre-pass: ch 2/30 chunk 1/3" in
        the status footer.
        """

        listener = self._batch_listener
        if listener is None:
            return
        try:
            listener.post_message(BatchPrePassTick(event))
        except Exception:  # pragma: no cover — defensive
            _logger.debug("listener no longer accepts pre-pass messages")

    def _on_worker_done(
        self,
        *,
        status: str,
        summary: BatchSummary | None,
        project_id: str,
        error: str | None = None,
    ) -> None:
        """Finalize: update the slot, notify the listener, drop the handle."""

        handle = self._batch_handle
        chapter_count = handle.chapter_count if handle is not None else 0
        starting_spend = handle.starting_spend_usd if handle is not None else 0.0
        total_segments = handle.total_segments if handle is not None else 0
        started_at = handle.started_at if handle is not None else None
        self.batch_progress = BatchProgress(
            active=False,
            project_id=project_id if status != "failed" else None,
            summary=summary,
            total=total_segments,
            chapter_count=chapter_count,
            starting_spend_usd=starting_spend,
            started_at=started_at,
            paused=status == "paused",
            cancelling=False,
        )
        listener = self._batch_listener
        # Drop the handle *after* we publish the final progress so a
        # late ``post_message`` racing with the finalizer still sees a
        # consistent snapshot. The listener (Dashboard) decides
        # whether to keep showing the panel.
        self._batch_handle = None
        if listener is not None:
            try:
                listener.post_message(
                    BatchFinished(
                        status=status,
                        summary=summary,
                        error=error,
                        project_id=project_id,
                    )
                )
            except Exception:  # pragma: no cover — listener gone
                _logger.debug("listener gone before BatchFinished delivered")
        # App-level toast so the curator gets feedback even when the
        # original Dashboard has been popped (the batch was started,
        # the curator navigated away, then the worker hit a problem).
        # The toast is non-blocking and auto-dismisses.
        self._notify_batch_done(status=status, error=error, summary=summary)

    def _notify_batch_done(
        self,
        *,
        status: str,
        error: str | None,
        summary: BatchSummary | None,
    ) -> None:
        """Surface a transient toast when a batch terminates.

        Routed through ``self.notify`` so it lands on whatever Screen
        is focused — by design, errors that happen "while you're
        looking somewhere else" should still be visible. We
        deliberately stay quiet for ``completed`` runs that didn't
        flag anything: a successful batch is the expected case and
        the rich progress panel already showed the curator the
        outcome.
        """

        try:
            if status == "failed":
                msg = "Batch failed"
                if error:
                    msg = f"{msg}: {error}"
                self.notify(msg, severity="error", title="Batch")
            elif status == "cancelled":
                self.notify("Batch cancelled.", severity="warning", title="Batch")
            elif status == "paused":
                self.notify(
                    "Batch paused (rate limit or budget cap). "
                    "Resume from the Dashboard.",
                    severity="warning",
                    title="Batch",
                )
            elif status == "completed" and summary is not None and summary.flagged > 0:
                self.notify(
                    f"Batch finished with {summary.flagged} flagged segment(s).",
                    severity="warning",
                    title="Batch",
                )
        except Exception:  # pragma: no cover — defensive in headless tests
            _logger.debug("notify failed for batch %s status", status)


def run(
    *,
    initial_screen: Screen[None] | None = None,
    config_path: Path | None = None,
    default_model: str | None = None,
) -> None:
    """Entry point used by :mod:`epublate.cli`."""

    EpublateApp(
        initial_screen=initial_screen,
        config_path=config_path,
        default_model=default_model,
    ).run()


__all__ = [
    "EPUBLATE_CONTRAST_THEME_NAME",
    "EPUBLATE_DEFAULT_THEME_NAME",
    "BatchFinished",
    "BatchProgress",
    "BatchTick",
    "EpublateApp",
    "ProviderFactory",
    "run",
]
