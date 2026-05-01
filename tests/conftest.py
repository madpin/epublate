"""Shared pytest fixtures.

Tests must run on a fresh clone with no network and no API keys (PRD NFR-7 /
testing rule). Anything that touches the network is mocked or banned.

Every test also runs inside an isolated XDG home so the user's real
``~/.config/epublate/recents.json`` is never mutated; the
:func:`_xdg_isolation` autouse fixture below enforces that and also
pins ``EPUBLATE_PROJECTS_ROOT`` so tests exercising the TUI's
new-project path don't create folders in the developer's Documents
directory.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest
from ebooklib import epub
from sqlalchemy.engine import Engine

from epublate.db import connect


@pytest.fixture(autouse=True)
def _xdg_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect every XDG-style lookup to a per-test tmp dir.

    Without this fixture CLI tests (which call ``_record_recent`` on
    success) leak project directories into the real
    ``~/.config/epublate/recents.json`` and the landing screen ends
    up full of ghosts. Autousing it costs one ``monkeypatch.setenv``
    per test and keeps the developer machine clean.
    """

    config_home = tmp_path / "xdg-config"
    data_home = tmp_path / "xdg-data"
    projects_root = tmp_path / "xdg-projects"
    config_home.mkdir(parents=True, exist_ok=True)
    data_home.mkdir(parents=True, exist_ok=True)
    projects_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("EPUBLATE_PROJECTS_ROOT", str(projects_root))


FIXTURES_DIR = Path(__file__).parent / "fixtures"
SAMPLE_EPUB = FIXTURES_DIR / "sample.epub"

XHTML_TEMPLATE = (
    "<?xml version='1.0' encoding='utf-8'?>"
    '<html xmlns="http://www.w3.org/1999/xhtml" '
    'xml:lang="{lang}" lang="{lang}">'
    "<head><title>{title}</title></head><body>{body}</body></html>"
)


@pytest.fixture
def project_db(tmp_path: Path) -> Iterator[Engine]:
    """A migrated, WAL-enabled SQLite engine for a throwaway project."""

    engine = connect(tmp_path / "demo.epublate")
    try:
        yield engine
    finally:
        engine.dispose()


def make_epub(
    out_path: Path,
    chapters: Sequence[tuple[str, str]],
    *,
    title: str = "Test Book",
    language: str = "en",
    identifier: str = "urn:epublate:test:fixture",
    author: str = "epublate test",
) -> Path:
    """Build a tiny valid ePub at ``out_path`` from ``[(title, body_xhtml), ...]``.

    Each ``body_xhtml`` is the inner HTML of the chapter's ``<body>`` (no
    wrapping ``<html>``). Used by tests to spin up minimal fixtures without
    relying on any external binary.
    """

    book = epub.EpubBook()
    book.set_identifier(identifier)
    book.set_title(title)
    book.set_language(language)
    book.add_author(author)

    items: list[epub.EpubHtml] = []
    for idx, (chap_title, body) in enumerate(chapters, start=1):
        uid = f"ch{idx:02d}"
        item = epub.EpubHtml(
            uid=uid,
            file_name=f"{uid}.xhtml",
            lang=language,
            title=chap_title,
        )
        item.content = XHTML_TEMPLATE.format(
            lang=language, title=chap_title, body=body
        ).encode("utf-8")
        book.add_item(item)
        items.append(item)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *items]
    book.toc = [epub.Link(item.file_name, item.title, item.id) for item in items]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    epub.write_epub(str(out_path), book)
    return out_path


@pytest.fixture
def tiny_epub_factory(
    tmp_path: Path,
) -> Callable[..., Path]:
    """Factory: ``factory(chapters, **kwargs) -> Path`` writes into ``tmp_path``."""

    counter = {"n": 0}

    def _factory(
        chapters: Sequence[tuple[str, str]] | None = None,
        *,
        title: str = "Test Book",
        language: str = "en",
        name: str = "tiny",
    ) -> Path:
        counter["n"] += 1
        chapters = chapters or [
            (
                "Chapter One",
                "<h1>Chapter One</h1>"
                "<p>Hello, <em>brave</em> world.</p>"
                '<p>Second paragraph with a <a href="#fn1">link</a>.</p>',
            ),
        ]
        out = tmp_path / f"{name}-{counter['n']}.epub"
        return make_epub(out, chapters, title=title, language=language)

    return _factory


@pytest.fixture(scope="session")
def sample_epub_path() -> Path:
    """The committed CC0 ePub used for the byte-realistic round-trip test."""

    if not SAMPLE_EPUB.is_file():
        raise pytest.UsageError(
            f"missing {SAMPLE_EPUB}; regenerate with "
            "`uv run python -m tests.fixtures._build_sample`"
        )
    return SAMPLE_EPUB
