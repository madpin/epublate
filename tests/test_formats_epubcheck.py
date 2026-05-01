"""Tests for the optional epubcheck wrapper (PRD F-IO-6 / M6).

These tests exercise the wrapper without depending on the upstream
``epubcheck`` PyPI package or a Java runtime — both are monkey-patched
into ``sys.modules`` so the test suite stays hermetic on every
contributor machine and on CI (PRD NFR-7).

A single integration-style test runs against the real wrapper and is
gated on ``EPUBLATE_EPUBCHECK_INTEGRATION=1`` so contributors can
opt in locally without slowing down the default loop.
"""

from __future__ import annotations

import os
import sys
import types
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from epublate.formats.epubcheck import (
    EpubCheckError,
    EpubCheckMessage,
    EpubCheckReport,
    is_available,
    run_epubcheck,
)


@dataclass
class _FakeMessage:
    id: str
    level: str
    location: str
    message: str
    suggestion: str | None = None


_LAST_CALL: dict[str, object] = {}


class _FakeEpubCheck:
    """Minimal stand-in for ``epubcheck.EpubCheck``."""

    def __init__(
        self,
        infile: str,
        *,
        lang: str = "en",
        autorun: bool = True,
        messages: Iterable[_FakeMessage] | None = None,
        raise_on_run: BaseException | None = None,
    ) -> None:
        _LAST_CALL["infile"] = infile
        _LAST_CALL["lang"] = lang
        self.infile = infile
        self.lang = lang
        if raise_on_run is not None:
            raise raise_on_run
        self.messages = list(messages or [])
        self.valid = not any(
            m.level in ("FATAL", "ERROR", "WARNING") for m in self.messages
        )


def _install_fake_module(
    monkeypatch: pytest.MonkeyPatch,
    *,
    messages: Iterable[_FakeMessage] = (),
    raise_on_run: BaseException | None = None,
) -> None:
    module = types.ModuleType("epubcheck")

    captured = list(messages)
    error = raise_on_run

    class _Bound(_FakeEpubCheck):
        def __init__(
            self, infile: str, *, lang: str = "en", autorun: bool = True
        ) -> None:
            super().__init__(
                infile,
                lang=lang,
                autorun=autorun,
                messages=captured,
                raise_on_run=error,
            )

    module.EpubCheck = _Bound  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "epubcheck", module)


@pytest.fixture
def java_present(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Pretend ``java`` is on PATH so the wrapper attempts to run."""

    monkeypatch.setattr("epublate.formats.epubcheck._java_available", lambda: True)
    yield


@pytest.fixture
def empty_epub(tmp_path: Path) -> Path:
    """A real file path so the wrapper progresses past its existence check."""

    p = tmp_path / "stub.epub"
    p.write_bytes(b"PK\x05\x06" + b"\x00" * 18)
    return p


def test_skipped_when_module_missing(
    monkeypatch: pytest.MonkeyPatch, empty_epub: Path
) -> None:
    monkeypatch.delitem(sys.modules, "epubcheck", raising=False)
    monkeypatch.setattr(
        "builtins.__import__",
        _import_minus_epubcheck(builtins_import=__import__),
    )
    report = run_epubcheck(empty_epub)
    assert report.status == "skipped"
    assert "extra" in (report.reason or "")


def test_skipped_when_java_missing(
    monkeypatch: pytest.MonkeyPatch, empty_epub: Path
) -> None:
    _install_fake_module(monkeypatch)
    monkeypatch.setattr("epublate.formats.epubcheck._java_available", lambda: False)
    report = run_epubcheck(empty_epub)
    assert report.status == "skipped"
    assert "java" in (report.reason or "").lower()


def test_skipped_when_file_missing(tmp_path: Path) -> None:
    report = run_epubcheck(tmp_path / "not-here.epub")
    assert report.status == "skipped"
    assert "not found" in (report.reason or "")


def test_passed_report_when_no_messages(
    monkeypatch: pytest.MonkeyPatch,
    java_present: None,
    empty_epub: Path,
) -> None:
    _install_fake_module(monkeypatch, messages=[])
    report = run_epubcheck(empty_epub)
    assert report.status == "passed"
    assert report.error_count == 0
    assert report.warning_count == 0
    assert "ok" in report.summary_line()


def test_failed_report_counts_errors_and_warnings(
    monkeypatch: pytest.MonkeyPatch,
    java_present: None,
    empty_epub: Path,
) -> None:
    _install_fake_module(
        monkeypatch,
        messages=[
            _FakeMessage(
                "OPF-049", "ERROR", "OPS/c1.xhtml:5:7", "missing entry", "fix it"
            ),
            _FakeMessage("RSC-011", "WARNING", "OPS/c1.xhtml:9", "broken link", None),
            _FakeMessage("ACC-001", "USAGE", "OPS/c2.xhtml", "alt text", None),
        ],
    )
    report = run_epubcheck(empty_epub)
    assert report.status == "failed"
    assert report.error_count == 1
    assert report.warning_count == 1
    # USAGE rolls into neither.
    assert len(report.messages) == 3
    assert report.has_errors is True


def test_run_epubcheck_passes_lang(
    monkeypatch: pytest.MonkeyPatch,
    java_present: None,
    empty_epub: Path,
) -> None:
    _install_fake_module(monkeypatch)
    _LAST_CALL.clear()
    run_epubcheck(empty_epub, lang="pt")
    assert _LAST_CALL.get("lang") == "pt"


def test_runner_crash_is_typed_error(
    monkeypatch: pytest.MonkeyPatch,
    java_present: None,
    empty_epub: Path,
) -> None:
    _install_fake_module(monkeypatch, raise_on_run=RuntimeError("boom"))
    with pytest.raises(EpubCheckError) as excinfo:
        run_epubcheck(empty_epub)
    assert "boom" in str(excinfo.value)


def test_event_payload_truncates_messages(
    monkeypatch: pytest.MonkeyPatch,
    java_present: None,
    empty_epub: Path,
) -> None:
    many = [
        _FakeMessage(f"E-{i:03d}", "ERROR", f"f.xhtml:{i}", "msg", None)
        for i in range(50)
    ]
    _install_fake_module(monkeypatch, messages=many)
    report = run_epubcheck(empty_epub)
    payload = report.event_payload()
    assert payload["error_count"] == 50
    assert isinstance(payload["messages"], list)
    assert len(payload["messages"]) == 20  # capped


def test_message_short_format() -> None:
    msg = EpubCheckMessage(
        id="OPF-049",
        level="ERROR",
        location="OPS/c1.xhtml:5:7",
        message="missing entry",
        suggestion="fix it",
    )
    assert msg.short() == "ERROR - OPF-049 - OPS/c1.xhtml:5:7 - missing entry"


def test_report_summary_line_for_skipped() -> None:
    report = EpubCheckReport(status="skipped", reason="extra missing")
    assert "extra missing" in report.summary_line()


def test_is_available_handles_missing_module(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delitem(sys.modules, "epubcheck", raising=False)
    monkeypatch.setattr(
        "builtins.__import__",
        _import_minus_epubcheck(builtins_import=__import__),
    )
    assert is_available() is False


@pytest.mark.skipif(
    os.environ.get("EPUBLATE_EPUBCHECK_INTEGRATION") != "1",
    reason="set EPUBLATE_EPUBCHECK_INTEGRATION=1 to exercise the real wrapper",
)
def test_integration_real_runner_against_sample(sample_epub_path: Path) -> None:
    report = run_epubcheck(sample_epub_path)
    assert report.status in ("passed", "failed", "skipped")


def _import_minus_epubcheck(*, builtins_import):  # type: ignore[no-untyped-def]
    """Build an ``__import__`` shim that hides the ``epubcheck`` module."""

    def _shim(name: str, *args: object, **kwargs: object) -> object:
        if name == "epubcheck" or name.startswith("epubcheck."):
            raise ImportError("epubcheck not installed")
        return builtins_import(name, *args, **kwargs)

    return _shim
