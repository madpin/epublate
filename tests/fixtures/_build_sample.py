"""Regenerate ``tests/fixtures/sample.epub`` from :mod:`sample_book`.

Run via:

    uv run python -m tests.fixtures._build_sample

The output is committed so the round-trip test exercises real ebooklib
bytes; rebuild whenever ``sample_book.py`` changes.
"""

from __future__ import annotations

from pathlib import Path

from ebooklib import epub

from tests.fixtures import sample_book

XHTML_HEAD = (
    "<?xml version='1.0' encoding='utf-8'?>"
    '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="{lang}" lang="{lang}">'
    "<head><title>{title}</title></head><body>{body}</body></html>"
)

OUT_PATH = Path(__file__).with_name("sample.epub")


def build() -> Path:
    book = epub.EpubBook()
    book.set_identifier(sample_book.IDENTIFIER)
    book.set_title(sample_book.TITLE)
    book.set_language(sample_book.LANGUAGE)
    book.add_author(sample_book.AUTHOR)

    chapters: list[epub.EpubHtml] = []
    for ch in sample_book.CHAPTERS:
        item = epub.EpubHtml(
            uid=ch.uid,
            file_name=ch.file_name,
            lang=sample_book.LANGUAGE,
            title=ch.title,
        )
        item.content = XHTML_HEAD.format(
            lang=sample_book.LANGUAGE, title=ch.title, body=ch.body_xhtml
        ).encode("utf-8")
        book.add_item(item)
        chapters.append(item)

    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = ["nav", *chapters]
    book.toc = [
        epub.Link(ch.file_name, ch.title, ch.uid) for ch in sample_book.CHAPTERS
    ]

    epub.write_epub(str(OUT_PATH), book)
    return OUT_PATH


if __name__ == "__main__":
    path = build()
    print(f"wrote {path} ({path.stat().st_size} bytes)")
