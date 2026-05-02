"""``epublate lore ...`` CLI surface (PRD §4.3 / F-LB-10)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from epublate.cli import main
from epublate.db import repo, schema
from epublate.glossary import io as glossary_io
from epublate.lore import LoreBook


def _new_book(runner: CliRunner, tmp_path: Path, *, name: str = "Series Lore") -> Path:
    out_dir = tmp_path / "lore.epublate-lore"
    result = runner.invoke(
        main,
        [
            "lore",
            "new",
            "--name",
            name,
            "--source-lang",
            "en",
            "--target-lang",
            "pt",
            "--out",
            str(out_dir),
            "--description",
            "Witcher proper-nouns canon",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Created Lore Book" in result.output
    return out_dir


def test_lore_new_creates_directory_and_db(tmp_path: Path) -> None:
    runner = CliRunner()
    out_dir = _new_book(runner, tmp_path)
    assert out_dir.is_dir()
    db = out_dir / "lore.epublate-lore"
    assert db.is_file()


def test_lore_open_prints_summary(tmp_path: Path) -> None:
    runner = CliRunner()
    out_dir = _new_book(runner, tmp_path)

    book = LoreBook.open(out_dir)
    try:
        repo.create_glossary_entry(
            book.engine,
            project_id=book.project_id,
            source_term="Geralt",
            target_term="Geralt",
            type="character",
            status=schema.GlossaryStatus.LOCKED,
        )
        repo.create_glossary_entry(
            book.engine,
            project_id=book.project_id,
            source_term=None,
            target_term="Yennefer de Vengerberg",
            type="character",
            status=schema.GlossaryStatus.PROPOSED,
            source_known=False,
        )
    finally:
        book.close()

    result = runner.invoke(main, ["lore", "open", str(out_dir)])
    assert result.exit_code == 0, result.output
    assert "Series Lore" in result.output
    assert "entries    : 2 (target-only: 1)" in result.output
    assert "locked    : 1" in result.output
    assert "proposed  : 1" in result.output


def test_lore_list_includes_new_book(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = CliRunner()
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setenv("EPUBLATE_LORE_LIBRARY", str(library))

    out_dir = library / "series.epublate-lore"
    result = runner.invoke(
        main,
        [
            "lore",
            "new",
            "--name",
            "Series Lore",
            "--source-lang",
            "en",
            "--target-lang",
            "pt",
            "--out",
            str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output

    listed = runner.invoke(main, ["lore", "list"])
    assert listed.exit_code == 0, listed.output
    assert str(library) in listed.output
    assert str(out_dir) in listed.output


def test_lore_export_then_import_round_trip(tmp_path: Path) -> None:
    runner = CliRunner()
    out_dir = _new_book(runner, tmp_path)

    book = LoreBook.open(out_dir)
    try:
        repo.create_glossary_entry(
            book.engine,
            project_id=book.project_id,
            source_term=None,
            target_term="Triss Merigold",
            type="character",
            status=schema.GlossaryStatus.LOCKED,
            source_known=False,
        )
    finally:
        book.close()

    export_path = tmp_path / "lore.json"
    exported = runner.invoke(
        main, ["lore", "export", str(out_dir), "--out", str(export_path)]
    )
    assert exported.exit_code == 0, exported.output
    payload = json.loads(export_path.read_text())
    assert payload["version"] == glossary_io.GLOSSARY_FORMAT_VERSION
    assert any(
        entry["target_term"] == "Triss Merigold" and entry["source_known"] is False
        for entry in payload["entries"]
    )

    other_dir = tmp_path / "other.epublate-lore"
    creating = runner.invoke(
        main,
        [
            "lore",
            "new",
            "--name",
            "Other Lore",
            "--source-lang",
            "en",
            "--target-lang",
            "pt",
            "--out",
            str(other_dir),
        ],
    )
    assert creating.exit_code == 0, creating.output

    imported = runner.invoke(
        main,
        [
            "lore",
            "import",
            str(other_dir),
            str(export_path),
            "--conflict",
            "skip",
        ],
    )
    assert imported.exit_code == 0, imported.output
    assert "1 created" in imported.output

    other_book = LoreBook.open(other_dir)
    try:
        entries = repo.list_glossary_entries(other_book.engine, other_book.project_id)
    finally:
        other_book.close()

    target_only = [e for e in entries if not e.source_known]
    assert len(target_only) == 1
    assert target_only[0].entry.target_term == "Triss Merigold"


def test_lore_ingest_source_with_mock_llm(
    tmp_path: Path,
    tiny_epub_factory: Callable[..., Path],
) -> None:
    runner = CliRunner()
    out_dir = _new_book(runner, tmp_path)

    epub_path = tiny_epub_factory(
        title="Series Book One",
        chapters=[
            (
                "Chapter One",
                "<h1>Chapter One</h1><p>Geralt rode into Kaer Morhen at dusk.</p>",
            ),
        ],
    )

    # Pre-seed the Lore Book with a deterministic mock response by
    # importing a JSON glossary first — this avoids relying on the
    # MockLLMProvider's plumbing through the CLI (which builds its
    # own provider instance) while still proving the command runs.
    starter = tmp_path / "starter.json"
    starter.write_text(
        json.dumps(
            {
                "version": glossary_io.GLOSSARY_FORMAT_VERSION,
                "entries": [
                    {
                        "source_term": "Geralt",
                        "target_term": "Geralt",
                        "type": "character",
                        "status": "proposed",
                        "source_known": True,
                    },
                ],
            }
        )
    )
    importing = runner.invoke(
        main,
        ["lore", "import", str(out_dir), str(starter), "--conflict", "skip"],
    )
    assert importing.exit_code == 0, importing.output

    # The actual ingest invocation only needs to print the
    # "Source ingest complete" header and exit cleanly under
    # ``--mock-llm`` (the mock provider's empty default response
    # yields zero proposals, which is fine for this smoke test).
    result = runner.invoke(
        main,
        [
            "--mock-llm",
            "lore",
            "ingest",
            "source",
            str(out_dir),
            str(epub_path),
            "--helper-model",
            "mock-helper",
            "--max-segments",
            "5",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Source ingest complete" in result.output


def _new_project_with_glossary(
    runner: CliRunner,
    tiny_epub_factory: Callable[..., Path],
    tmp_path: Path,
    *,
    name: str,
    entries: list[dict[str, object]],
) -> Path:
    src = tiny_epub_factory(
        chapters=[("Chap", "<h1>Chap</h1><p>Hello.</p>")],
        title=name,
    )
    out_dir = tmp_path / name
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
    from epublate.core.project import Project

    project = Project.open(out_dir)
    try:
        for entry in entries:
            repo.create_glossary_entry(
                project.engine,
                project_id=project.project_id,
                source_term=entry.get("source_term"),
                target_term=entry["target_term"],  # type: ignore[arg-type]
                type=entry.get("type", "character"),  # type: ignore[arg-type]
                status=entry.get("status", "confirmed"),  # type: ignore[arg-type]
                source_known=entry.get(
                    "source_known", entry.get("source_term") is not None
                ),  # type: ignore[arg-type]
            )
    finally:
        project.close()
    return out_dir


def test_lore_new_with_from_project_bootstraps_entries(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    runner = CliRunner()
    project_dir = _new_project_with_glossary(
        runner,
        tiny_epub_factory,
        tmp_path,
        name="book1",
        entries=[
            {
                "source_term": "Geralt",
                "target_term": "Geralt de Rívia",
                "status": "locked",
            },
            {
                "source_term": "Yennefer",
                "target_term": "Yennefer",
                "status": "confirmed",
            },
        ],
    )
    out_dir = tmp_path / "lore.epublate-lore"
    result = runner.invoke(
        main,
        [
            "lore",
            "new",
            "--name",
            "Witcher Lore",
            "--source-lang",
            "en",
            "--target-lang",
            "pt",
            "--out",
            str(out_dir),
            "--from-project",
            str(project_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Created Lore Book" in result.output
    assert "bootstrap : imported 2 entries" in result.output

    book = LoreBook.open(out_dir)
    try:
        entries = repo.list_glossary_entries(book.engine, book.project_id)
    finally:
        book.close()
    sources = sorted(e.source_term for e in entries if e.source_term)
    assert sources == ["Geralt", "Yennefer"]


def test_lore_import_project_skip_keeps_destination(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    runner = CliRunner()
    project_dir = _new_project_with_glossary(
        runner,
        tiny_epub_factory,
        tmp_path,
        name="book2",
        entries=[
            {
                "source_term": "Geralt",
                "target_term": "Geralt the Witcher",
                "status": "confirmed",
            },
        ],
    )
    out_dir = _new_book(runner, tmp_path)
    book = LoreBook.open(out_dir)
    try:
        repo.create_glossary_entry(
            book.engine,
            project_id=book.project_id,
            source_term="Geralt",
            target_term="Geralt de Rívia",
            type="character",
            status=schema.GlossaryStatus.LOCKED,
        )
    finally:
        book.close()

    result = runner.invoke(
        main,
        [
            "lore",
            "import-project",
            str(out_dir),
            str(project_dir),
            "--on-conflict",
            "skip",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "skipped   : 1" in result.output

    reopen = LoreBook.open(out_dir)
    try:
        existing = repo.find_glossary_entry_by_source_term(
            reopen.engine,
            project_id=reopen.project_id,
            source_term="Geralt",
            type="character",
        )
        assert existing is not None
        assert existing.target_term == "Geralt de Rívia"
    finally:
        reopen.close()


def test_lore_import_project_overwrite_replaces_destination(
    tiny_epub_factory: Callable[..., Path], tmp_path: Path
) -> None:
    runner = CliRunner()
    project_dir = _new_project_with_glossary(
        runner,
        tiny_epub_factory,
        tmp_path,
        name="book3",
        entries=[
            {
                "source_term": "Geralt",
                "target_term": "Geralt the Witcher",
                "status": "confirmed",
            },
        ],
    )
    out_dir = _new_book(runner, tmp_path)
    book = LoreBook.open(out_dir)
    try:
        repo.create_glossary_entry(
            book.engine,
            project_id=book.project_id,
            source_term="Geralt",
            target_term="Geralt de Rívia",
            type="character",
            status=schema.GlossaryStatus.LOCKED,
        )
    finally:
        book.close()

    result = runner.invoke(
        main,
        [
            "lore",
            "import-project",
            str(out_dir),
            str(project_dir),
            "--on-conflict",
            "overwrite",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "updated   : 1" in result.output

    reopen = LoreBook.open(out_dir)
    try:
        existing = repo.find_glossary_entry_by_source_term(
            reopen.engine,
            project_id=reopen.project_id,
            source_term="Geralt",
            type="character",
        )
        assert existing is not None
        assert existing.target_term == "Geralt the Witcher"
    finally:
        reopen.close()
