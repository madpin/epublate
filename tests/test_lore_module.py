"""Lore Book primitive — lifecycle + repo + library smoke tests."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from epublate.db import repo, schema
from epublate.errors import ConfigurationError
from epublate.lore import (
    DEFAULT_LIBRARY_ENV,
    LoreBook,
    create_lore_meta,
    default_library_dir,
    get_lore_meta,
    insert_lore_source,
    iter_library_lore_books,
    list_lore_sources,
    update_lore_meta,
)


def _make_lore_book(tmp_path: Path, *, name: str = "Witcher Lore") -> LoreBook:
    return LoreBook.create(
        out_dir=tmp_path / f"{name.lower().replace(' ', '_')}.epublate-lore",
        name=name,
        source_lang="en",
        target_lang="pt",
        description="Series-wide lore for the Witcher novels",
    )


def test_create_lore_book_writes_layout(tmp_path: Path) -> None:
    book = _make_lore_book(tmp_path)
    try:
        assert book.lore_dir.exists()
        assert book.db_path.is_file()
        assert (book.lore_dir / "sources").is_dir()
        meta = get_lore_meta(book.engine, project_id=book.project_id)
        assert meta is not None
        assert meta.description == "Series-wide lore for the Witcher novels"
        assert meta.default_proposal_kind == schema.LoreSourceKind.TARGET
        # The underlying project row is marked as ``kind='lore'`` so the
        # LoreBooksScreen filter can find it without scanning the disk.
        rows = repo.list_projects(book.engine, kind=schema.ProjectKind.LORE)
        assert [r.id for r in rows] == [book.project_id]
        assert repo.list_projects(book.engine, kind=schema.ProjectKind.BOOK) == []
    finally:
        book.close()


def test_open_lore_book_round_trips(tmp_path: Path) -> None:
    book = _make_lore_book(tmp_path, name="Fellowship Lore")
    project_id = book.project_id
    book.close()

    reopened = LoreBook.open(book.lore_dir)
    try:
        assert reopened.project_id == project_id
        assert reopened.name == "Fellowship Lore"
        assert reopened.source_lang == "en"
        assert reopened.target_lang == "pt"
    finally:
        reopened.close()


def test_create_refuses_non_empty_dir(tmp_path: Path) -> None:
    out_dir = tmp_path / "lore"
    out_dir.mkdir()
    (out_dir / "junk.txt").write_text("hi")
    with pytest.raises(ConfigurationError):
        LoreBook.create(
            out_dir=out_dir,
            name="X",
            source_lang="en",
            target_lang="pt",
        )


def test_open_rejects_directory_without_lore_db(tmp_path: Path) -> None:
    out_dir = tmp_path / "empty"
    out_dir.mkdir()
    with pytest.raises(ConfigurationError):
        LoreBook.open(out_dir)


def test_create_lore_meta_round_trip(tmp_path: Path) -> None:
    book = _make_lore_book(tmp_path, name="Round Trip")
    try:
        meta = get_lore_meta(book.engine, project_id=book.project_id)
        assert meta is not None
        assert meta.schema_version == 1
        assert meta.default_proposal_kind == schema.LoreSourceKind.TARGET
    finally:
        book.close()


def test_update_lore_meta_patches_fields(tmp_path: Path) -> None:
    book = _make_lore_book(tmp_path, name="Patchy")
    try:
        updated = update_lore_meta(
            book.engine,
            project_id=book.project_id,
            description="Updated description",
            default_proposal_kind=schema.LoreSourceKind.SOURCE,
        )
        assert updated.description == "Updated description"
        assert updated.default_proposal_kind == schema.LoreSourceKind.SOURCE
    finally:
        book.close()


def test_create_lore_meta_directly_for_arbitrary_project(tmp_path: Path) -> None:
    """Repo helper works on a bare project row too (used by tests)."""

    book = _make_lore_book(tmp_path, name="Direct")
    try:
        # Insert an extra project (kind=book) and attach a meta row to
        # confirm the helper doesn't enforce kind.
        extra = repo.create_project(
            book.engine,
            name="Sidecar",
            source_lang="en",
            target_lang="es",
            source_path="",
            kind=schema.ProjectKind.BOOK,
        )
        meta = create_lore_meta(
            book.engine,
            project_id=extra.id,
            description="ad-hoc",
            default_proposal_kind=schema.LoreSourceKind.SOURCE,
        )
        assert meta.project_id == extra.id
        assert get_lore_meta(book.engine, project_id=extra.id) is not None
    finally:
        book.close()


def test_insert_lore_source_records_audit_row(tmp_path: Path) -> None:
    book = _make_lore_book(tmp_path, name="Sources")
    try:
        row = insert_lore_source(
            book.engine,
            project_id=book.project_id,
            kind=schema.LoreSourceKind.SOURCE,
            epub_path=str(tmp_path / "fake.epub"),
            entries_added=5,
            notes="smoke test",
        )
        assert row.entries_added == 5
        listed = list_lore_sources(book.engine, project_id=book.project_id)
        assert [r.id for r in listed] == [row.id]
    finally:
        book.close()


def test_insert_lore_source_rejects_unknown_kind(tmp_path: Path) -> None:
    book = _make_lore_book(tmp_path, name="Reject")
    try:
        with pytest.raises(ValueError):
            insert_lore_source(
                book.engine,
                project_id=book.project_id,
                kind="bogus",
                epub_path="x",
            )
    finally:
        book.close()


def test_default_library_dir_honours_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(DEFAULT_LIBRARY_ENV, "/tmp/elsewhere")
    assert default_library_dir() == Path("/tmp/elsewhere").resolve()


def test_default_library_dir_falls_back_to_xdg_config_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(DEFAULT_LIBRARY_ENV, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert default_library_dir() == (tmp_path / "xdg" / "epublate" / "lore").resolve()


def test_default_library_dir_falls_back_to_home(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.delenv(DEFAULT_LIBRARY_ENV, raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    assert default_library_dir() == tmp_path / ".config" / "epublate" / "lore"


def test_iter_library_lore_books_finds_books(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setenv(DEFAULT_LIBRARY_ENV, str(library))

    book = LoreBook.create(
        out_dir=library / "first.epublate-lore",
        name="First",
        source_lang="en",
        target_lang="pt",
    )
    book.close()

    handles = list(iter_library_lore_books())
    assert [h.name for h in handles] == ["first.epublate-lore"]
    assert handles[0].db_path.is_file()


def test_iter_library_skips_directories_without_lore_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = tmp_path / "library"
    library.mkdir()
    (library / "junk").mkdir()
    monkeypatch.setenv(DEFAULT_LIBRARY_ENV, str(library))
    assert list(iter_library_lore_books()) == []


def test_iter_library_returns_empty_when_dir_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEFAULT_LIBRARY_ENV, str(tmp_path / "missing"))
    assert list(iter_library_lore_books()) == []


def test_lore_book_stash_source_copies_epub(
    tmp_path: Path,
    tiny_epub_factory: Callable[..., Path],
) -> None:
    book = _make_lore_book(tmp_path, name="Stasher")
    try:
        epub_path = tiny_epub_factory(name="stash")
        dest = book.stash_source(epub_path)
        assert dest.exists()
        assert dest.parent == book.sources_dir
        # Stashing again gets a unique name so we don't clobber history.
        again = book.stash_source(epub_path)
        assert again != dest
        assert again.exists()
    finally:
        book.close()


def test_lore_book_stash_source_rejects_missing_file(tmp_path: Path) -> None:
    book = _make_lore_book(tmp_path, name="Missing")
    try:
        with pytest.raises(ConfigurationError):
            book.stash_source(tmp_path / "nope.epub")
    finally:
        book.close()
