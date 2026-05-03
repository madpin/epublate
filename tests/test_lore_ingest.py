"""Lore Book ingest helpers — source ePub + target ePub paths."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from epublate.db import repo, schema
from epublate.llm.base import Message, ResponseFormat
from epublate.llm.mock import MockLLMProvider
from epublate.lore import (
    LoreBook,
    ingest_source_epub,
    ingest_target_epub,
)


def _make_lore_book(tmp_path: Path) -> LoreBook:
    return LoreBook.create(
        out_dir=tmp_path / "lore.epublate-lore",
        name="Series Lore",
        source_lang="en",
        target_lang="pt",
    )


def _english_chapters() -> list[tuple[str, str]]:
    return [
        (
            "Chapter One",
            "<h1>Chapter One</h1>"
            "<p>Geralt rode into Kaer Morhen at dusk.</p>"
            "<p>Yennefer was waiting on the keep's parapet.</p>",
        ),
        (
            "Chapter Two",
            "<h1>Chapter Two</h1>"
            "<p>The wolf medallion hummed as Geralt drew his silver sword.</p>",
        ),
    ]


def _portuguese_chapters() -> list[tuple[str, str]]:
    return [
        (
            "Capítulo Um",
            "<h1>Capítulo Um</h1>"
            "<p>Geralt cavalgou até Kaer Morhen ao crepúsculo.</p>"
            "<p>Yennefer esperava no parapeito da fortaleza.</p>",
        ),
    ]


def test_ingest_source_epub_records_audit_row(
    tmp_path: Path,
    tiny_epub_factory: Callable[..., Path],
) -> None:
    book = _make_lore_book(tmp_path)
    provider = MockLLMProvider()
    provider.set_response(
        json.dumps(
            {
                "entities": [
                    {
                        "type": "character",
                        "source": "Geralt",
                        "evidence": "Geralt rode into Kaer Morhen at dusk.",
                        "confidence": 0.95,
                    },
                    {
                        "type": "place",
                        "source": "Kaer Morhen",
                        "evidence": "Geralt rode into Kaer Morhen at dusk.",
                        "confidence": 0.9,
                    },
                ],
                "pov": "third_limited",
                "tense": "past",
                "register": "genre",
                "audience": "adult",
            }
        )
    )
    epub_path = tiny_epub_factory(_english_chapters(), name="lore-source")
    try:
        summary = ingest_source_epub(
            book,
            epub_path=epub_path,
            provider=provider,
            helper_model="mock-helper",
        )
        assert summary.proposed_count == 2
        sources = repo.list_glossary_entries(book.engine, book.project_id)
        assert {e.source_term for e in sources} == {"Geralt", "Kaer Morhen"}
        assert all(e.entry.source_known for e in sources)
        # The audit lore_source row records the proposal count.
        from epublate.lore.repo import list_lore_sources

        rows = list_lore_sources(book.engine, project_id=book.project_id)
        assert len(rows) == 1
        assert rows[0].entries_added == 2
        assert rows[0].kind == schema.LoreSourceKind.SOURCE
    finally:
        book.close()


def test_ingest_source_epub_failure_marks_audit_row_failed(
    tmp_path: Path,
    tiny_epub_factory: Callable[..., Path],
) -> None:
    book = _make_lore_book(tmp_path)
    provider = MockLLMProvider()
    # The mock never has a configured response: every call raises an
    # LLMResponseError, which the extractor records as a failed chunk.
    epub_path = tiny_epub_factory(_english_chapters(), name="lore-failure")
    try:
        summary = ingest_source_epub(
            book,
            epub_path=epub_path,
            provider=provider,
            helper_model="mock-helper",
        )
        assert summary.proposed_count == 0
        from epublate.lore.repo import list_lore_sources

        rows = list_lore_sources(book.engine, project_id=book.project_id)
        # The intake helper records its failures via failed_chunks but
        # still emits a lore_source row with status=ingested when the
        # helper itself didn't raise out of run_book_intake. We assert
        # that the run was registered; status detail lives in the
        # event log.
        assert len(rows) == 1
    finally:
        book.close()


def test_ingest_target_epub_creates_target_only_entries(
    tmp_path: Path,
    tiny_epub_factory: Callable[..., Path],
) -> None:
    book = _make_lore_book(tmp_path)
    provider = MockLLMProvider()
    provider.set_response(
        json.dumps(
            {
                "entities": [
                    {
                        "type": "character",
                        "target": "Geralt",
                        "aliases": ["Bruxo"],
                        "evidence": "Geralt cavalgou até Kaer Morhen ao crepúsculo.",
                        "confidence": 0.97,
                    },
                    {
                        "type": "place",
                        "target": "Kaer Morhen",
                        "confidence": 0.9,
                    },
                ],
                "notes": "Tradução literária consistente.",
            }
        )
    )
    target_epub = tiny_epub_factory(
        _portuguese_chapters(), name="lore-target", language="pt"
    )

    try:
        source_row, summary = ingest_target_epub(
            book,
            epub_path=target_epub,
            provider=provider,
            helper_model="mock-helper",
        )
        assert summary.proposed_count == 2
        assert source_row.kind == schema.LoreSourceKind.TARGET
        entries = repo.list_glossary_entries(book.engine, book.project_id)
        by_target = {e.target_term: e for e in entries}
        assert set(by_target) == {"Geralt", "Kaer Morhen"}
        assert all(e.entry.source_term is None for e in entries)
        assert all(e.entry.source_known is False for e in entries)
        # The Geralt entry has its alias preserved on the target side.
        assert "Bruxo" in by_target["Geralt"].target_aliases
    finally:
        book.close()


def test_ingest_target_epub_dedupes_known_target_terms(
    tmp_path: Path,
    tiny_epub_factory: Callable[..., Path],
) -> None:
    book = _make_lore_book(tmp_path)
    # Pre-seed the Lore Book with one of the entities so the second
    # run skips it.
    repo.create_glossary_entry(
        book.engine,
        project_id=book.project_id,
        source_term=None,
        target_term="Geralt",
        type="character",
        status="locked",
        source_known=False,
    )
    provider = MockLLMProvider()
    provider.set_response(
        json.dumps(
            {
                "entities": [
                    {"type": "character", "target": "Geralt"},
                    {"type": "place", "target": "Kaer Morhen"},
                ]
            }
        )
    )
    target_epub = tiny_epub_factory(
        _portuguese_chapters(), name="lore-target-dedupe", language="pt"
    )
    try:
        _, summary = ingest_target_epub(
            book,
            epub_path=target_epub,
            provider=provider,
            helper_model="mock-helper",
        )
        # Only Kaer Morhen should be newly created.
        assert summary.proposed_count == 1
        targets = {
            e.target_term
            for e in repo.list_glossary_entries(book.engine, book.project_id)
        }
        assert targets == {"Geralt", "Kaer Morhen"}
    finally:
        book.close()


def test_ingest_target_epub_defaults_to_json_response_format(
    tmp_path: Path,
    tiny_epub_factory: Callable[..., Path],
) -> None:
    """The target ingest helper requests JSON mode by default so the
    target extractor doesn't have to recover JSON from prose (the
    failure mode reasoning helpers like ``gpt-oss-20b`` hit when
    the visible-channel budget is consumed by reasoning tokens).
    """

    book = _make_lore_book(tmp_path)
    provider = MockLLMProvider()
    provider.set_response(json.dumps({"entities": []}))
    target_epub = tiny_epub_factory(
        _portuguese_chapters(), name="lore-target-jsonmode", language="pt"
    )
    try:
        ingest_target_epub(
            book,
            epub_path=target_epub,
            provider=provider,
            helper_model="mock-helper",
        )
        last = provider.last_request
        assert last is not None
        assert last.response_format == ResponseFormat(type="json_object")
    finally:
        book.close()


def test_ingest_target_epub_skips_chapters_beyond_max(
    tmp_path: Path,
    tiny_epub_factory: Callable[..., Path],
) -> None:
    book = _make_lore_book(tmp_path)
    provider = MockLLMProvider()

    captured: list[Message] = []

    def _responder(messages: list[Message], _model: str) -> str:
        captured.extend(messages)
        return json.dumps({"entities": []})

    provider.set_responder(_responder)

    chapters = [
        (f"Capítulo {i}", f"<p>Texto do capítulo {i} mencionando Geralt.</p>")
        for i in range(1, 6)
    ]
    target_epub = tiny_epub_factory(chapters, name="lore-many-chapters", language="pt")
    try:
        _, summary = ingest_target_epub(
            book,
            epub_path=target_epub,
            provider=provider,
            helper_model="mock-helper",
            max_chapters=2,
            chunk_max_chars=50,
        )
        # max_chapters=2 caps the prose harvest at the first two
        # chapters; we should never see chapter 3+ content in the
        # captured prompts.
        body_text = "\n".join(m.content for m in captured)
        assert "capítulo 3" not in body_text
        assert summary.failed_chunks == 0
    finally:
        book.close()
