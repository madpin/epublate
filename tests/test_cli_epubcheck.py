"""CLI surface for the epubcheck integration (PRD F-IO-6 / M6)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

import epublate.core.project as project_module
from epublate.cli import main
from epublate.formats.epubcheck import EpubCheckMessage, EpubCheckReport


def _new(runner: CliRunner, src: Path, out_dir: Path) -> None:
    res = runner.invoke(
        main,
        [
            "new",
            str(src),
            "--out",
            str(out_dir),
            "--source-lang",
            "en",
            "--target-lang",
            "pt",
        ],
    )
    assert res.exit_code == 0, res.output


def _patch_run_epubcheck(
    monkeypatch: pytest.MonkeyPatch, report: EpubCheckReport
) -> None:
    monkeypatch.setattr(
        project_module,
        "run_epubcheck",
        lambda epub_path, *, lang="en": report,
    )


def test_export_default_does_not_invoke_epubcheck(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    src = tiny_epub_factory()
    out_dir = tmp_path / "proj"
    _new(runner, src, out_dir)

    calls: list[Path] = []
    monkeypatch.setattr(
        project_module,
        "run_epubcheck",
        lambda epub_path, *, lang="en": calls.append(epub_path),
    )

    result = runner.invoke(
        main,
        ["export", str(out_dir), "--out", str(tmp_path / "out.epub")],
    )
    assert result.exit_code == 0, result.output
    assert calls == []
    assert "epubcheck" not in result.output.lower()


def test_export_with_epubcheck_emits_summary(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    src = tiny_epub_factory()
    out_dir = tmp_path / "proj"
    _new(runner, src, out_dir)

    _patch_run_epubcheck(
        monkeypatch,
        EpubCheckReport(status="passed"),
    )

    result = runner.invoke(
        main,
        [
            "export",
            str(out_dir),
            "--out",
            str(tmp_path / "out.epub"),
            "--epubcheck",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "epubcheck ok" in result.output


def test_export_strict_succeeds_when_passed(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    src = tiny_epub_factory()
    out_dir = tmp_path / "proj"
    _new(runner, src, out_dir)

    _patch_run_epubcheck(monkeypatch, EpubCheckReport(status="passed"))

    result = runner.invoke(
        main,
        [
            "export",
            str(out_dir),
            "--out",
            str(tmp_path / "out.epub"),
            "--strict",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "epubcheck ok" in result.output


def test_export_strict_fails_when_errors(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    src = tiny_epub_factory()
    out_dir = tmp_path / "proj"
    _new(runner, src, out_dir)

    _patch_run_epubcheck(
        monkeypatch,
        EpubCheckReport(
            status="failed",
            error_count=1,
            warning_count=0,
            messages=(
                EpubCheckMessage(
                    id="OPF-049",
                    level="ERROR",
                    location="OPS/c1.xhtml:5:7",
                    message="missing entry",
                ),
            ),
        ),
    )

    result = runner.invoke(
        main,
        [
            "export",
            str(out_dir),
            "--out",
            str(tmp_path / "out.epub"),
            "--strict",
        ],
    )
    assert result.exit_code == 3, result.output
    out = result.output
    assert "epubcheck failed" in out
    assert "OPF-049" in out


def test_export_strict_warns_when_skipped(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    src = tiny_epub_factory()
    out_dir = tmp_path / "proj"
    _new(runner, src, out_dir)

    _patch_run_epubcheck(
        monkeypatch,
        EpubCheckReport(status="skipped", reason="extra missing"),
    )

    result = runner.invoke(
        main,
        [
            "export",
            str(out_dir),
            "--out",
            str(tmp_path / "out.epub"),
            "--strict",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "epubcheck skipped" in result.output
    assert "[epubcheck] extra" in result.output
