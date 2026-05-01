"""Tests for ``Project.export(epubcheck=True)`` (PRD F-IO-6 / M6)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

import epublate.core.project as project_module
from epublate.core.project import Project
from epublate.db import repo
from epublate.formats.epubcheck import EpubCheckMessage, EpubCheckReport


def _patch_run_epubcheck(
    monkeypatch: pytest.MonkeyPatch, report: EpubCheckReport
) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake(epub_path: Path, *, lang: str = "en") -> EpubCheckReport:
        captured["epub_path"] = epub_path
        captured["lang"] = lang
        return report

    monkeypatch.setattr(project_module, "run_epubcheck", fake)
    return captured


def _make_project(tiny_factory: Callable[..., Path], tmp_path: Path) -> Project:
    src = tiny_factory(chapters=[("Solo", "<p>Hello, world.</p>")])
    return Project.create(
        src, out_dir=tmp_path / "p", source_lang="en", target_lang="pt"
    )


def test_export_skips_epubcheck_when_flag_off(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        captured = _patch_run_epubcheck(monkeypatch, EpubCheckReport(status="passed"))
        out_path = tmp_path / "out.epub"
        project.export(out_path)
        assert out_path.is_file()
        assert captured == {}
        assert project.last_epubcheck_report is None
    finally:
        project.close()


def test_export_runs_epubcheck_and_records_event(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        report = EpubCheckReport(
            status="passed",
            error_count=0,
            warning_count=0,
        )
        captured = _patch_run_epubcheck(monkeypatch, report)
        out_path = tmp_path / "out.epub"
        project.export(out_path, epubcheck=True)

        assert captured["epub_path"] == out_path
        assert project.last_epubcheck_report is report

        events = [
            ev
            for ev in repo.list_events(project.engine, project.project_id)
            if ev.kind == "project.epubcheck_completed"
        ]
        assert len(events) == 1
        payload = events[0].payload
        assert payload["status"] == "passed"
        assert payload["error_count"] == 0
        assert payload["warning_count"] == 0
    finally:
        project.close()


def test_export_records_failed_report(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = _make_project(tiny_epub_factory, tmp_path)
    try:
        report = EpubCheckReport(
            status="failed",
            error_count=1,
            warning_count=2,
            messages=(
                EpubCheckMessage(
                    id="OPF-049",
                    level="ERROR",
                    location="OPS/c1.xhtml:5:7",
                    message="missing entry",
                ),
            ),
        )
        _patch_run_epubcheck(monkeypatch, report)
        out_path = tmp_path / "out.epub"
        # Even with errors, the export still completes — strict mode is the
        # CLI's call, not the core's.
        project.export(out_path, epubcheck=True)
        assert out_path.is_file()
        assert project.last_epubcheck_report is report
    finally:
        project.close()
