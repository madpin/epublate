"""Optional ``epubcheck`` integration (PRD F-IO-6 / M6).

The epubcheck integration is **opt-in** via the ``[epubcheck]`` extra:

    uv tool install epublate[epubcheck]
    # or for development
    uv sync --extra epubcheck

Behavior (resolves PRD §11 open question #4):

* warn-only by default — every export runs epubcheck if it's available
  and surfaces the count of errors/warnings; non-zero counts never
  block the export.
* ``epublate export --strict`` flips to hard-fail mode: errors abort
  the export with a non-zero exit code (the file is still written
  atomically, so the curator can inspect it).

When the extra isn't installed, or Java isn't on the system, we return
a ``SKIPPED`` :class:`EpubCheckReport` instead of crashing — the export
itself never depends on the validator.

The wrapper class deliberately doesn't expose every field of the
underlying ``epubcheck.EpubCheck`` object; we only surface what the
TUI and CLI need (counts, severities, a small log) so we're not
coupled to upstream's namedtuple shape.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from epublate.errors import FormatError

_logger = logging.getLogger(__name__)

EpubCheckStatus = Literal["passed", "failed", "skipped"]
EpubCheckSeverity = Literal["FATAL", "ERROR", "WARNING", "USAGE", "INFO"]

_ERROR_LEVELS: frozenset[str] = frozenset({"FATAL", "ERROR"})
_WARNING_LEVELS: frozenset[str] = frozenset({"WARNING"})
_MAX_LOG_MESSAGES = 20


class EpubCheckError(FormatError):
    """The epubcheck wrapper *was* available but its execution blew up.

    Distinct from "skipped": ``skipped`` means we deliberately couldn't
    run (extra missing, JRE missing); ``EpubCheckError`` means we ran
    and the runner itself crashed (e.g. malformed JSON output, Java
    abort), which the curator should know about.
    """


@dataclass(slots=True, frozen=True)
class EpubCheckMessage:
    """One issue surfaced by epubcheck."""

    id: str
    level: EpubCheckSeverity
    location: str
    message: str
    suggestion: str | None = None

    def short(self) -> str:
        return f"{self.level} - {self.id} - {self.location} - {self.message}"


@dataclass(slots=True, frozen=True)
class EpubCheckReport:
    """Outcome of running epubcheck against an exported file.

    ``status``:

    * ``passed``  — epubcheck ran and reported zero errors and zero
      warnings (info / usage messages are still allowed).
    * ``failed``  — epubcheck ran and reported at least one error or
      warning. The curator may still want to ship the file; callers
      decide whether to treat ``failed`` as fatal (``--strict`` does;
      the default doesn't, per PRD F-IO-6 / open question #4).
    * ``skipped`` — the integration is opt-in and the user hasn't
      installed it (or Java isn't available). ``reason`` carries a
      one-line explanation suitable for the CLI and event log.
    """

    status: EpubCheckStatus
    error_count: int = 0
    warning_count: int = 0
    messages: tuple[EpubCheckMessage, ...] = field(default_factory=tuple)
    reason: str | None = None

    @property
    def ran(self) -> bool:
        return self.status != "skipped"

    @property
    def has_errors(self) -> bool:
        return self.error_count > 0

    def summary_line(self) -> str:
        if self.status == "skipped":
            tail = f": {self.reason}" if self.reason else ""
            return f"epubcheck skipped{tail}"
        return (
            f"epubcheck {'ok' if not self.has_errors else 'failed'} "
            f"({self.error_count} errors, {self.warning_count} warnings)"
        )

    def event_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "error_count": self.error_count,
            "warning_count": self.warning_count,
            "reason": self.reason or "",
            "messages": [m.short() for m in self.messages[:_MAX_LOG_MESSAGES]],
        }


def is_available() -> bool:
    """``True`` iff the ``epubcheck`` extra and a JRE are both reachable."""

    try:
        import epubcheck  # type: ignore[import-not-found]  # noqa: F401
    except ImportError:
        return False
    return _java_available()


def run_epubcheck(
    epub_path: Path,
    *,
    lang: str = "en",
) -> EpubCheckReport:
    """Validate ``epub_path`` and return a structured report.

    Never raises on a missing dependency — those paths return a
    ``skipped`` report. Raises :class:`EpubCheckError` only if the
    runner is present but blows up while parsing its own output (a
    real bug worth surfacing to the curator).
    """

    epub_path = Path(epub_path)
    if not epub_path.is_file():
        return EpubCheckReport(
            status="skipped",
            reason=f"file not found: {epub_path}",
        )

    try:
        import epubcheck as _ec
    except ImportError:
        return EpubCheckReport(
            status="skipped",
            reason="install the [epubcheck] extra to enable validation",
        )

    if not _java_available():
        return EpubCheckReport(
            status="skipped",
            reason="java not found on PATH (epubcheck wrapper requires a JRE)",
        )

    try:
        result = _ec.EpubCheck(str(epub_path), lang=lang)
    except FileNotFoundError as exc:
        return EpubCheckReport(
            status="skipped",
            reason=f"epubcheck runner missing prerequisite: {exc}",
        )
    except Exception as exc:
        raise EpubCheckError(f"epubcheck runner crashed: {exc}") from exc

    return _build_report(result)


def _build_report(result: object) -> EpubCheckReport:
    raw_messages = getattr(result, "messages", None) or []
    messages = tuple(_coerce_messages(raw_messages))
    error_count = sum(1 for m in messages if m.level in _ERROR_LEVELS)
    warning_count = sum(1 for m in messages if m.level in _WARNING_LEVELS)
    status: EpubCheckStatus = (
        "passed" if (error_count == 0 and warning_count == 0) else "failed"
    )
    return EpubCheckReport(
        status=status,
        error_count=error_count,
        warning_count=warning_count,
        messages=messages,
    )


def _coerce_messages(raw: Iterable[object]) -> Iterable[EpubCheckMessage]:
    for m in raw:
        try:
            level = str(getattr(m, "level", "INFO")).upper()
            yield EpubCheckMessage(
                id=str(getattr(m, "id", "")),
                level=level if level in _ALL_LEVELS else "INFO",  # type: ignore[arg-type]
                location=str(getattr(m, "location", "")),
                message=str(getattr(m, "message", "")),
                suggestion=(str(getattr(m, "suggestion", "") or "") or None),
            )
        except Exception:  # defend against shape drift
            _logger.debug("skipped malformed epubcheck message: %r", m)


_ALL_LEVELS: frozenset[str] = frozenset({"FATAL", "ERROR", "WARNING", "USAGE", "INFO"})


def _java_available() -> bool:
    """Quick probe for a JRE on ``PATH``.

    The ``epubcheck`` wrapper shells out to ``java -jar`` in its own
    constructor; we want to detect "no JRE installed" *before* hitting
    that and ending up with an unhelpful ``FileNotFoundError``.
    """

    import shutil

    return shutil.which("java") is not None


__all__ = [
    "EpubCheckError",
    "EpubCheckMessage",
    "EpubCheckReport",
    "EpubCheckSeverity",
    "EpubCheckStatus",
    "is_available",
    "run_epubcheck",
]
