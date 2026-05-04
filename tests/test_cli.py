"""CLI surface (PRD NFR-7 / `uv run epublate ...`)."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from epublate import __version__
from epublate.cli import main
from epublate.core.project import Project
from epublate.db import repo
from epublate.llm.mock import MockLLMProvider


def test_version_subcommand() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_version_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.output


def test_mock_llm_flag_sets_environment() -> None:
    runner = CliRunner()
    os.environ.pop("EPUBLATE_LLM", None)
    result = runner.invoke(main, ["--mock-llm", "version"])
    assert result.exit_code == 0
    assert os.environ.get("EPUBLATE_LLM") == "mock"


def test_bare_invocation_plumbs_env_model_to_tui(
    monkeypatch: object,
) -> None:
    """Bare ``epublate`` resolves ``$EPUBLATE_LLM_MODEL`` for the TUI.

    Regression for the asymmetry where ``epublate open`` honored the
    env var but the bare TUI silently used the hardcoded
    ``DEFAULT_MODEL`` — a confusing footgun once ``.env`` loading was
    enabled.
    """

    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> None:
        captured.update(kwargs)

    import epublate.app.main as app_main

    monkeypatch.setattr(app_main, "run", fake_run)  # type: ignore[attr-defined]
    monkeypatch.setenv("EPUBLATE_LLM_MODEL", "gpt-5-free")  # type: ignore[attr-defined]

    runner = CliRunner()
    result = runner.invoke(main, [])
    assert result.exit_code == 0, result.output
    assert captured.get("default_model") == "gpt-5-free"


def test_bare_invocation_falls_back_to_default_model_when_env_unset(
    monkeypatch: object,
) -> None:
    """No ``$EPUBLATE_LLM_MODEL`` → fall back to the Reader's default."""

    captured: dict[str, object] = {}

    def fake_run(**kwargs: object) -> None:
        captured.update(kwargs)

    import epublate.app.main as app_main
    from epublate.app.screens.reader import DEFAULT_MODEL

    monkeypatch.setattr(app_main, "run", fake_run)  # type: ignore[attr-defined]
    monkeypatch.delenv("EPUBLATE_LLM_MODEL", raising=False)  # type: ignore[attr-defined]

    runner = CliRunner()
    result = runner.invoke(main, [])
    assert result.exit_code == 0, result.output
    assert captured.get("default_model") == DEFAULT_MODEL


def test_new_creates_project_dir(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    runner = CliRunner()
    src = tiny_epub_factory()
    out_dir = tmp_path / "myproj"
    result = runner.invoke(
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
    assert result.exit_code == 0, result.output
    assert (out_dir / "original.epub").is_file()
    assert any(out_dir.glob("*.epublate"))


def test_new_requires_target_lang(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    runner = CliRunner()
    src = tiny_epub_factory()
    result = runner.invoke(main, ["new", str(src), "--out", str(tmp_path / "p")])
    assert result.exit_code != 0
    assert "--target-lang" in result.output


def test_new_defaults_out_to_projects_root(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting ``--out`` lands the project under ``EPUBLATE_PROJECTS_ROOT``."""

    runner = CliRunner()
    src = tiny_epub_factory()
    projects_root = tmp_path / "roots"
    projects_root.mkdir()
    monkeypatch.setenv("EPUBLATE_PROJECTS_ROOT", str(projects_root))
    result = runner.invoke(
        main,
        [
            "new",
            str(src),
            "--source-lang",
            "en",
            "--target-lang",
            "pt",
        ],
    )
    assert result.exit_code == 0, result.output
    # Exactly one project dir under the root, named after the source stem.
    children = list(projects_root.iterdir())
    assert len(children) == 1
    assert children[0].name == src.stem
    assert (children[0] / "original.epub").is_file()


def test_new_default_deduplicates_when_root_occupied(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second ``epublate new`` with the same stem lands in ``<stem>-2``."""

    runner = CliRunner()
    src = tiny_epub_factory()
    projects_root = tmp_path / "roots"
    projects_root.mkdir()
    monkeypatch.setenv("EPUBLATE_PROJECTS_ROOT", str(projects_root))

    first = runner.invoke(
        main,
        ["new", str(src), "--source-lang", "en", "--target-lang", "pt"],
    )
    assert first.exit_code == 0, first.output

    second = runner.invoke(
        main,
        ["new", str(src), "--source-lang", "en", "--target-lang", "pt"],
    )
    assert second.exit_code == 0, second.output
    names = sorted(p.name for p in projects_root.iterdir())
    assert names == sorted([src.stem, f"{src.stem}-2"])


def test_export_round_trips(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    runner = CliRunner()
    src = tiny_epub_factory()
    out_dir = tmp_path / "proj"
    out_epub = tmp_path / "out.epub"

    new_result = runner.invoke(
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
    assert new_result.exit_code == 0, new_result.output

    export_result = runner.invoke(
        main, ["export", str(out_dir), "--out", str(out_epub)]
    )
    assert export_result.exit_code == 0, export_result.output
    assert out_epub.is_file()


def test_repair_subcommand_reports_no_op_on_fresh_project(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """``epublate repair`` is a curator-facing surface — keep the line stable.

    The summary string is what the curator sees in the terminal, so a
    no-op project must produce exactly the "0 added, 0 re-hosted"
    breakdown plus the explicit ``up to date`` follow-up. If we ever
    rephrase those lines, this test must change in lock-step.
    """

    runner = CliRunner()
    src = tiny_epub_factory()
    out_dir = tmp_path / "proj"

    new_result = runner.invoke(
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
    assert new_result.exit_code == 0, new_result.output

    repair_result = runner.invoke(main, ["repair", str(out_dir)])
    assert repair_result.exit_code == 0, repair_result.output
    assert (
        "0 new segment(s) inserted, 0 existing row(s) re-hosted" in repair_result.output
    )
    assert "already up to date" in repair_result.output


def test_open_subcommand_loads_project(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    runner = CliRunner()
    src = tiny_epub_factory()
    out_dir = tmp_path / "proj"
    runner.invoke(
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
    # ``EPUBLATE_NO_TUI=1`` short-circuits the Reader before booting
    # Textual; we just want to assert the project loads cleanly.
    os.environ["EPUBLATE_NO_TUI"] = "1"
    try:
        result = runner.invoke(main, ["--mock-llm", "open", str(out_dir)])
    finally:
        os.environ.pop("EPUBLATE_NO_TUI", None)
    assert result.exit_code == 0, result.output
    assert "Opening Dashboard on" in result.output


def test_open_subcommand_passes_custom_model(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    runner = CliRunner()
    src = tiny_epub_factory()
    out_dir = tmp_path / "proj"
    runner.invoke(
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
    os.environ["EPUBLATE_NO_TUI"] = "1"
    try:
        result = runner.invoke(
            main,
            ["--mock-llm", "open", str(out_dir), "--model", "gpt-5-mini"],
        )
    finally:
        os.environ.pop("EPUBLATE_NO_TUI", None)
    assert result.exit_code == 0, result.output
    assert "model=gpt-5-mini" in result.output


# ---------------------------------------------------------------------------
# M4 commands
# ---------------------------------------------------------------------------


def _new_project_dir(
    runner: CliRunner, tiny_factory: Callable[..., Path], tmp_path: Path
) -> Path:
    src = tiny_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1><p>Hello, world.</p><p>Second paragraph.</p>",
            ),
        ]
    )
    out_dir = tmp_path / "proj"
    result = runner.invoke(
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
    assert result.exit_code == 0, result.output
    return out_dir


def _patched_build_provider(monkeypatch_obj: object, provider: MockLLMProvider) -> None:
    """Pin :func:`epublate.llm.factory.build_provider` to ``provider``."""

    import epublate.cli as cli_module
    import epublate.llm.factory as factory

    def _factory(*, mock: bool = False) -> MockLLMProvider:
        del mock
        return provider

    monkeypatch_obj.setattr(factory, "build_provider", _factory)  # type: ignore[attr-defined]
    monkeypatch_obj.setattr(cli_module, "build_provider", _factory, raising=False)  # type: ignore[attr-defined]


def test_batch_command_runs_with_mock_llm(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)

    provider = MockLLMProvider()

    def _responder(messages: list[object], _model: str) -> str:
        last = messages[-1]
        content = getattr(last, "content", "")
        return json.dumps({"target": f"PT::{content}"})

    provider.set_responder(_responder)
    _patched_build_provider(monkeypatch, provider)

    result = runner.invoke(
        main, ["--mock-llm", "batch", str(out_dir), "--model", "gpt-mock"]
    )

    assert result.exit_code == 0, result.output
    assert "Batch complete" in result.output
    assert "translated" in result.output


def test_batch_command_pauses_on_budget_cap(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)

    from epublate.llm.pricing import ModelPrice, reset_prices, set_price

    provider = MockLLMProvider()

    def _responder(messages: list[object], _model: str) -> str:
        last = messages[-1]
        return json.dumps({"target": f"PT::{getattr(last, 'content', '')}"})

    provider.set_responder(_responder)
    _patched_build_provider(monkeypatch, provider)

    set_price(
        "gpt-mock",
        ModelPrice(input_per_mtok=1000.0, output_per_mtok=1000.0),
    )

    try:
        result = runner.invoke(
            main,
            [
                "--mock-llm",
                "batch",
                str(out_dir),
                "--model",
                "gpt-mock",
                "--budget",
                "0.0001",
            ],
        )
    finally:
        reset_prices()

    assert result.exit_code == 2, result.output
    assert "Batch paused" in result.output


def test_inbox_command_lists_sections(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)

    project = Project.open(out_dir)
    try:
        chap = repo.list_chapters(project.engine, project.project_id)[0]
        seg = repo.list_segments(project.engine, chap.id)[0]
        repo.update_segment_translation(
            project.engine,
            segment_id=seg.id,
            target_text="bad",
            status="flagged",
        )
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Élise",
            target_term="Élise",
            type="character",
            status="proposed",
        )
        repo.append_event(
            project.engine,
            project_id=project.project_id,
            kind="batch.completed",
            payload={"translated": 1},
        )
    finally:
        project.close()

    result = runner.invoke(main, ["inbox", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert "Flagged segments" in result.output
    assert "Proposed glossary entries" in result.output
    assert "Recent alerts" in result.output


def test_stats_command_emits_json(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)

    result = runner.invoke(main, ["stats", str(out_dir), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["spend_usd"] == 0.0
    assert payload["budget_usd"] is None
    assert payload["segment_count"] > 0


def test_glossary_cleanup_years_dry_run_lists_only(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Without ``--apply`` the command lists candidates and exits.

    The dry-run default is intentional: glossary deletes are not
    cheap to undo (the row is gone, with all its mentions). The
    curator should always see the match list before deleting, so
    the CLI defaults to read-only and prints a "rerun with --apply"
    nudge.
    """

    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)
    project = Project.open(out_dir)
    try:
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="1066",
            target_term="1066",
            type="date_or_time",
            status="proposed",
        )
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Élise",
            target_term="Elisa",
            type="character",
            status="proposed",
        )
    finally:
        project.close()

    result = runner.invoke(main, ["glossary", "cleanup-years", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert "1066" in result.output
    assert "Élise" not in result.output
    assert "Dry run" in result.output

    # Without --apply, nothing should have been deleted.
    project = Project.open(out_dir)
    try:
        proposed = repo.list_glossary_entries(
            project.engine, project.project_id, status="proposed"
        )
        sources = {e.source_term for e in proposed}
        assert "1066" in sources
        assert "Élise" in sources
    finally:
        project.close()


def test_glossary_cleanup_years_apply_deletes_year_entries(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """``--apply`` actually removes year-like proposed entries.

    Locked / confirmed entries are NEVER touched (they were
    promoted by the curator on purpose); only proposed entries with
    a year-like source term are eligible. A confirmed year entry in
    the fixture acts as a tripwire: if cleanup nukes it, the
    curator's manual decisions get steam-rolled.
    """

    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)
    project = Project.open(out_dir)
    try:
        # Year-like proposed entries — should be deleted.
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="1066",
            target_term="1066",
            type="date_or_time",
            status="proposed",
        )
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="1939-1945",
            target_term="1939-1945",
            type="date_or_time",
            status="proposed",
        )
        # Confirmed year entry (curator-promoted) — must survive.
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="2024",
            target_term="2024",
            type="date_or_time",
            status="confirmed",
        )
        # Real proposed entity — must survive.
        repo.create_glossary_entry(
            project.engine,
            project_id=project.project_id,
            source_term="Élise",
            target_term="Elisa",
            type="character",
            status="proposed",
        )
    finally:
        project.close()

    result = runner.invoke(main, ["glossary", "cleanup-years", str(out_dir), "--apply"])
    assert result.exit_code == 0, result.output
    assert "Deleted 2" in result.output

    project = Project.open(out_dir)
    try:
        all_entries = repo.list_glossary_entries(project.engine, project.project_id)
        sources = {e.source_term for e in all_entries}
        assert "1066" not in sources
        assert "1939-1945" not in sources
        # Confirmed and real proposed entries survive.
        assert "2024" in sources
        assert "Élise" in sources
    finally:
        project.close()


def test_sanitize_typography_dry_run_lists_only(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """Without ``--apply`` the command lists candidates and exits.

    Mirror of the dry-run pattern in ``glossary cleanup-years``:
    rewriting target_text on segments is a structural change to
    the project DB that the curator should always preview before
    committing. The default is read-only.
    """

    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)
    project = Project.open(out_dir)
    try:
        chap = repo.list_chapters(project.engine, project.project_id)[0]
        seg = repo.list_segments(project.engine, chap.id)[0]
        repo.update_segment_translation(
            project.engine,
            segment_id=seg.id,
            target_text="Eu \u2019tenho uma desculpa.",
            status="translated",
        )
    finally:
        project.close()

    result = runner.invoke(main, ["sanitize-typography", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert "Found 1 segment" in result.output
    assert "Eu \u2019tenho" in result.output
    assert "Eu tenho" in result.output
    assert "Dry run" in result.output

    project = Project.open(out_dir)
    try:
        # Without --apply the row must still hold the bad bytes.
        rows = repo.list_segments(project.engine, chap.id)
        assert any(s.target_text == "Eu \u2019tenho uma desculpa." for s in rows)
    finally:
        project.close()


def test_sanitize_typography_apply_rewrites_segments(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """``--apply`` rewrites the bad rows and preserves status.

    Status is a curator-controlled flag (locked / flagged /
    translated). The sanitiser is a pure-text rewrite and MUST
    not touch the status column — a flagged segment stays flagged
    after the rewrite, so the curator's review queue isn't
    silently emptied.
    """

    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)
    project = Project.open(out_dir)
    try:
        chap = repo.list_chapters(project.engine, project.project_id)[0]
        segs = repo.list_segments(project.engine, chap.id)
        # First segment: bad bytes, translated status — should be cleaned.
        repo.update_segment_translation(
            project.engine,
            segment_id=segs[0].id,
            target_text="Eu \u2019tenho uma desculpa.",
            status="translated",
        )
        # Second segment: bad bytes, flagged status — should be cleaned
        # AND keep its flagged status.
        if len(segs) > 1:
            repo.update_segment_translation(
                project.engine,
                segment_id=segs[1].id,
                target_text="por \u2019ter dedicado este livro",
                status="flagged",
            )
    finally:
        project.close()

    result = runner.invoke(main, ["sanitize-typography", str(out_dir), "--apply"])
    assert result.exit_code == 0, result.output
    assert "Rewrote" in result.output

    project = Project.open(out_dir)
    try:
        rows = repo.list_segments(project.engine, chap.id)
        cleaned = {s.target_text for s in rows if s.target_text}
        assert "Eu tenho uma desculpa." in cleaned
        # No row should still contain a curly apostrophe.
        for r in rows:
            if r.target_text:
                assert "\u2019" not in r.target_text
        # Verify status preserved on the flagged row, if it exists.
        if any(s.status == "flagged" for s in rows):
            flagged_after = [s for s in rows if s.status == "flagged"]
            assert flagged_after  # status preserved on rewrite
    finally:
        project.close()


def test_sanitize_typography_no_op_message(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """When there's nothing to clean, the output says so plainly."""

    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)
    result = runner.invoke(main, ["sanitize-typography", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output


def test_glossary_cleanup_years_no_op_message(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    """When there's nothing to clean up, the command says so plainly.

    Curators run this command speculatively after the auto-proposer
    upgrade; an empty result must not look like a hang or a silent
    failure.
    """

    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)
    result = runner.invoke(main, ["glossary", "cleanup-years", str(out_dir)])
    assert result.exit_code == 0
    assert "No proposed glossary entries match" in result.output


def test_budget_set_and_clear_round_trip(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)

    set_result = runner.invoke(main, ["budget", "set", str(out_dir), "12.50"])
    assert set_result.exit_code == 0, set_result.output
    assert "Budget set to $12.5000" in set_result.output

    show_result = runner.invoke(main, ["budget", "show", str(out_dir)])
    assert show_result.exit_code == 0
    assert "$12.5000" in show_result.output

    clear_result = runner.invoke(main, ["budget", "clear", str(out_dir)])
    assert clear_result.exit_code == 0
    assert "Budget cleared." in clear_result.output

    project = Project.open(out_dir)
    try:
        row = repo.get_project(project.engine, project.project_id)
        assert row is not None
        assert row.budget_usd is None
    finally:
        project.close()


# ---------------------------------------------------------------------------
# M5 commands (book intake & extractor)
# ---------------------------------------------------------------------------


def test_new_with_intake_runs_helper_and_proposes_entries(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    runner = CliRunner()
    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1>"
                "<p>Élise opened the door of Vale Verde.</p>"
                "<p>The Order of the Coffer watched silently.</p>",
            )
        ]
    )

    provider = MockLLMProvider()
    provider.set_response(
        json.dumps(
            {
                "entities": [
                    {"type": "character", "source": "Élise"},
                    {"type": "place", "source": "Vale Verde"},
                ],
                "pov": "third_limited",
            }
        )
    )
    _patched_build_provider(monkeypatch, provider)

    out_dir = tmp_path / "intake-proj"
    result = runner.invoke(
        main,
        [
            "--mock-llm",
            "new",
            str(src),
            "--out",
            str(out_dir),
            "--source-lang",
            "en",
            "--target-lang",
            "pt",
            "--intake",
            "--helper-model",
            "gpt-mock-helper",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "intake:" in result.output
    assert "proposed entries" in result.output
    assert provider.call_count >= 1

    project = Project.open(out_dir)
    try:
        proposed = repo.list_glossary_entries(
            project.engine, project.project_id, status="proposed"
        )
        sources = {p.source_term for p in proposed}
        assert {"Élise", "Vale Verde"} <= sources
    finally:
        project.close()


def test_intake_command_runs_on_existing_project(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)

    provider = MockLLMProvider()
    provider.set_response(
        json.dumps(
            {"entities": [{"type": "character", "source": "Hero"}], "pov": "first"}
        )
    )
    _patched_build_provider(monkeypatch, provider)

    result = runner.invoke(
        main,
        [
            "--mock-llm",
            "intake",
            str(out_dir),
            "--helper-model",
            "gpt-mock-helper",
            "--max-segments",
            "5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Intake complete" in result.output
    assert "proposed" in result.output

    project = Project.open(out_dir)
    try:
        proposed = repo.list_glossary_entries(
            project.engine, project.project_id, status="proposed"
        )
        assert any(p.source_term == "Hero" for p in proposed)
    finally:
        project.close()


def test_intake_command_surfaces_helper_style_suggestion(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """``epublate intake`` must print the helper's tone suggestion (PRD F-STYLE-3).

    With the helper reporting ``audience=children``, the suggester
    proposes the ``children_picture`` preset; the CLI surfaces it so a
    curator who skipped the New Project dialog still hears the
    suggestion.
    """

    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)

    provider = MockLLMProvider()
    provider.set_response(
        json.dumps(
            {
                "entities": [{"type": "character", "source": "Hero"}],
                "pov": "third_limited",
                "register": "literary",
                "audience": "children",
            }
        )
    )
    _patched_build_provider(monkeypatch, provider)

    result = runner.invoke(
        main,
        [
            "--mock-llm",
            "intake",
            str(out_dir),
            "--helper-model",
            "gpt-mock-helper",
            "--max-segments",
            "5",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "register" in result.output
    assert "audience" in result.output
    assert "suggested tone" in result.output
    assert "children_picture" in result.output


def test_new_with_intake_surfaces_helper_style_suggestion(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """``epublate new --intake`` mentions the suggestion when it differs.

    The default tone is ``literary_fiction``; the helper reports
    ``audience=children`` → ``children_picture``. Because the
    suggestion differs from the active preset, the CLI prints a
    "helper suggests tone" line so the curator can swap presets.
    """

    runner = CliRunner()
    src = tiny_epub_factory(
        chapters=[
            (
                "Solo",
                "<h1>Solo</h1><p>Once upon a time, in a small village.</p>",
            )
        ]
    )

    provider = MockLLMProvider()
    provider.set_response(
        json.dumps(
            {
                "entities": [{"type": "character", "source": "Hero"}],
                "pov": "third_limited",
                "register": "literary",
                "audience": "children",
            }
        )
    )
    _patched_build_provider(monkeypatch, provider)

    out_dir = tmp_path / "intake-suggest"
    result = runner.invoke(
        main,
        [
            "--mock-llm",
            "new",
            str(src),
            "--out",
            str(out_dir),
            "--source-lang",
            "en",
            "--target-lang",
            "pt",
            "--intake",
            "--helper-model",
            "gpt-mock-helper",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "helper observations" in result.output
    assert "helper suggests tone" in result.output
    assert "children_picture" in result.output


def test_batch_command_extract_flag_runs_pre_pass(
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    runner = CliRunner()
    out_dir = _new_project_dir(runner, tiny_epub_factory, tmp_path)

    provider = MockLLMProvider()

    def _responder(messages: list[object], _model: str) -> str:
        system = getattr(messages[0], "content", "")
        user = getattr(messages[-1], "content", "")
        if "Existing glossary" in system:
            return json.dumps({"entities": [{"type": "place", "source": "Vale Verde"}]})
        return json.dumps(
            {
                "target": f"PT::{user}",
                "used_entries": [],
                "new_entities": [],
            }
        )

    provider.set_responder(_responder)
    _patched_build_provider(monkeypatch, provider)

    result = runner.invoke(
        main,
        [
            "--mock-llm",
            "batch",
            str(out_dir),
            "--model",
            "gpt-mock",
            "--extract",
            "--helper-model",
            "gpt-mock-helper",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Batch complete" in result.output
    assert "pre_pass" in result.output

    project = Project.open(out_dir)
    try:
        proposed = repo.list_glossary_entries(
            project.engine, project.project_id, status="proposed"
        )
        assert any(p.source_term == "Vale Verde" for p in proposed)
    finally:
        project.close()
