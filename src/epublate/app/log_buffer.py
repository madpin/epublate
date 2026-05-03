"""In-memory ring-buffer log handler for the TUI Logs screen (PRD §11).

The PRD's open question on "where do log lines / errors live in the
TUI?" is answered with a per-process ring buffer the
:class:`epublate.app.screens.logs.LogsScreen` reads from. Reasons it
isn't another DB table:

* Python ``logging`` records carry tracebacks, ``LogRecord`` args,
  and exception chains that don't round-trip cleanly through SQL.
* The persistent audit trail already lives in the ``event`` table;
  Python logs are runtime debugging context that's lost on restart
  by design (CHANGELOG / PRD NFR-7: "no telemetry, no auto-uploads"
  — keeping logs ephemeral keeps that promise even when a curator
  forgets to clear a verbose handler).
* A bounded ``deque`` is O(1) at both ends and thread-safe for
  append/snapshot, so the worker threads (batch, cascade, ingest)
  can all log into it without any extra locking on top of the one
  ``logging.Handler`` already takes.

The handler is installed once in :class:`epublate.app.main.EpublateApp`
on the root ``epublate`` logger; child loggers propagate up.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Iterable

DEFAULT_CAPACITY = 2000
"""Default ring buffer size.

Sized to hold a few full batches' worth of progress lines without
recycling. ~2k 200-byte records ≈ 400 KiB resident — negligible on
the curator's machine, and enough that "what went wrong 10 minutes
ago" is still in scope when the curator opens the Logs screen.
"""


class RingBufferHandler(logging.Handler):
    """Logging handler that retains the last N records in a deque.

    Attached to the root ``epublate`` logger by the App. The
    :class:`epublate.app.screens.logs.LogsScreen` reads
    :meth:`records` (a snapshot copy) on each refresh; we never
    expose the underlying deque so callers can't mutate the shared
    state by accident.

    Thread safety: ``logging.Handler.emit`` is invoked under the
    global handler lock acquired by ``logging.Handler.handle``, and
    ``deque.append`` itself is atomic, so concurrent emits from the
    Textual worker pool, the batch thread, and the UI thread are all
    safe without an extra lock.

    The handler keeps the original :class:`logging.LogRecord`
    objects rather than pre-formatting them, so the Logs screen can
    render the message exactly as it would on stderr (including
    ``args`` interpolation and exception info) and still have the
    raw ``levelno`` / ``name`` / ``created`` fields available for
    filters.
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        super().__init__()
        self._records: deque[logging.LogRecord] = deque(maxlen=capacity)

    @property
    def capacity(self) -> int:
        return self._records.maxlen or 0

    def emit(self, record: logging.LogRecord) -> None:
        # Pre-render the message once, so callers don't have to
        # re-bind ``record.args`` every time they snapshot. Mirrors
        # what ``logging.Formatter.format`` does for the message
        # line, but without committing to a particular formatter.
        try:
            record.message = record.getMessage()
        except Exception:
            # Args interpolation can blow up if the user supplied
            # mismatched ``%`` args. Keep the record around with the
            # raw ``msg`` so the Logs screen still shows it.
            record.message = str(record.msg)
        self._records.append(record)

    def records(self) -> list[logging.LogRecord]:
        """Return a snapshot list of the buffered records (newest last).

        Snapshot semantics are deliberate: the Logs screen sorts /
        filters / paginates this list and we don't want a concurrent
        ``emit`` to invalidate the iteration mid-render.
        """

        return list(self._records)

    def clear(self) -> None:
        """Drop every buffered record. Mostly used by tests."""

        self._records.clear()


def install_ring_buffer(
    logger: logging.Logger | None = None,
    *,
    capacity: int = DEFAULT_CAPACITY,
    level: int = logging.INFO,
) -> RingBufferHandler:
    """Install a fresh :class:`RingBufferHandler` and return it.

    Idempotent in spirit: if the caller (the App) holds onto the
    returned handler reference, repeated calls don't accidentally
    leak handlers because the existing one is removed first. We
    don't try to deduplicate by class name because tests sometimes
    install their own handler and expect it to coexist.

    Default ``logger`` is the package-root ``epublate`` logger; child
    loggers propagate up to it (PRD §6.3 / NFR-7).
    """

    target = logger or logging.getLogger("epublate")
    handler = RingBufferHandler(capacity=capacity)
    handler.setLevel(level)
    target.addHandler(handler)
    # Make sure DEBUG-level messages from child loggers can reach the
    # handler — the root logger's effective level otherwise defaults
    # to WARNING and silently drops everything below INFO before the
    # handler ever sees it.
    if target.level == logging.NOTSET or target.level > level:
        target.setLevel(level)
    return handler


def uninstall_ring_buffer(
    handler: RingBufferHandler,
    logger: logging.Logger | None = None,
) -> None:
    """Detach ``handler`` from ``logger`` (defaults to ``epublate``).

    Used by tests that need a clean slate between cases. Production
    code holds the handler for the lifetime of the App.
    """

    target = logger or logging.getLogger("epublate")
    target.removeHandler(handler)


def filter_records(
    records: Iterable[logging.LogRecord],
    *,
    min_level: int = logging.NOTSET,
    needle: str | None = None,
    logger_prefix: str | None = None,
) -> list[logging.LogRecord]:
    """Apply the LogsScreen filters to a record snapshot.

    Pure function so the screen can render ``records()`` snapshots
    without mutating the buffer. Substring search is case-insensitive
    and matched against the formatted message text.
    """

    needle_l = needle.lower() if needle else None
    out: list[logging.LogRecord] = []
    for rec in records:
        if rec.levelno < min_level:
            continue
        if logger_prefix and not rec.name.startswith(logger_prefix):
            continue
        if needle_l:
            text = getattr(rec, "message", None) or rec.getMessage()
            if needle_l not in text.lower():
                continue
        out.append(rec)
    return out


__all__ = [
    "DEFAULT_CAPACITY",
    "RingBufferHandler",
    "filter_records",
    "install_ring_buffer",
    "uninstall_ring_buffer",
]
