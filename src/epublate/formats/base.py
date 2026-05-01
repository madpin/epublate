"""Format adapter Protocol and shared value objects (PRD §6.5).

The pipeline is format-agnostic: every format (ePub now, PDF later) implements
:class:`FormatAdapter`. Inline tags never reach the LLM — they are replaced
with opaque ``[[T0]]…[[/T0]]`` placeholders before any model call and restored
on reassembly (format-handling rule).

This module owns the *data types* crossing module boundaries; the ePub adapter
(:mod:`epublate.formats.epub`) owns the I/O against ``ebooklib`` / ``lxml``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

InlineKind = Literal["pair", "void"]


class InlineToken(BaseModel):
    """One inline tag captured from the source DOM.

    Stored in :attr:`Segment.inline_skeleton` and referenced positionally by
    placeholder index. ``pair`` tags wrap text (``<em>old</em>`` →
    ``[[T0]]old[[/T0]]``); ``void`` tags are self-closing (``<br/>`` →
    ``[[T0]]``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    tag: str
    kind: InlineKind
    attrs: dict[str, str] = Field(default_factory=dict)


class Segment(BaseModel):
    """A translation unit produced by :meth:`FormatAdapter.segment`.

    The ``source_text`` is the user-visible string with inline tags replaced by
    placeholders; ``inline_skeleton`` is the ordered list of tokens those
    placeholders refer to. ``host_path`` locates the block-level element the
    segment belongs to so :meth:`FormatAdapter.reassemble` knows where to
    splice the (possibly translated) text back into the DOM.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    chapter_id: str
    idx: int
    source_text: str
    source_hash: str
    target_text: str | None = None
    inline_skeleton: list[InlineToken] = Field(default_factory=list)
    host_path: str
    host_part: int = 0
    host_total_parts: int = 1


@dataclass(slots=True)
class ChapterDoc:
    """One spine document of a book, parsed and ready to mutate.

    The ``tree`` is an ``lxml`` element tree the adapter mutates in place
    during reassembly; ``raw_xml`` keeps the original bytes for documents the
    pipeline opted to skip (untranslatable items still need to be written
    verbatim into the output container).
    """

    spine_idx: int
    href: str
    title: str | None
    media_type: str
    raw_xml: bytes
    tree: Any | None = None


@dataclass(slots=True)
class Book:
    """Opaque handle to a loaded book.

    ``container`` is the format-specific runtime object (e.g. the
    ``ebooklib.epub.EpubBook``). The pipeline never inspects it directly — it
    only flows back into the same adapter for :meth:`FormatAdapter.save`.
    """

    path: Path
    title: str
    language: str
    container: Any
    extras: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class FormatAdapter(Protocol):
    """Format-agnostic ePub/PDF I/O surface (PRD §6.5)."""

    name: str

    def load(self, path: Path) -> Book: ...

    def iter_chapters(self, book: Book) -> Iterable[ChapterDoc]: ...

    def segment(
        self,
        doc: ChapterDoc,
        *,
        chapter_id: str,
        max_tokens: int = 800,
    ) -> list[Segment]: ...

    def reassemble(self, doc: ChapterDoc, translated: list[Segment]) -> ChapterDoc: ...

    def save(self, book: Book, out_path: Path) -> None: ...


__all__ = [
    "Book",
    "ChapterDoc",
    "FormatAdapter",
    "InlineKind",
    "InlineToken",
    "Segment",
]
